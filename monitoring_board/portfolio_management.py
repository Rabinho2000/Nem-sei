from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


STRONG_SUGGESTION_THRESHOLD = 0.92
POSSIBLE_SUGGESTION_THRESHOLD = 0.78
AMBIGUOUS_DELTA = 0.06

MAPPING_METHOD_NIF_EXACT = "nif_exact"
MAPPING_METHOD_ALIAS_EXACT = "alias_exact"
MAPPING_METHOD_NAME_EXACT = "name_exact"
MAPPING_METHOD_ALIAS_NORMALIZED = "alias_normalized"
MAPPING_METHOD_NAME_NORMALIZED = "name_normalized"
MAPPING_METHOD_ALIAS_FUZZY = "alias_fuzzy"
MAPPING_METHOD_NAME_FUZZY = "name_fuzzy"
MAPPING_METHOD_MANUAL = "manual"
MAPPING_METHOD_UNMAPPED = "unmapped"
MAPPING_METHOD_CONFLICT = "conflict"

SAFE_AUTO_METHODS = {
    MAPPING_METHOD_NIF_EXACT,
    MAPPING_METHOD_ALIAS_EXACT,
    MAPPING_METHOD_NAME_EXACT,
    MAPPING_METHOD_ALIAS_NORMALIZED,
    MAPPING_METHOD_NAME_NORMALIZED,
}

COMPANY_SUFFIXES = (
    ("unipessoal", "lda"),
    ("sociedade", "anonima"),
    ("sociedade", "por", "quotas"),
    ("l", "da"),
    ("s", "a"),
    ("c", "r", "l"),
    ("lda",),
    ("sa",),
    ("unipessoal",),
    ("sl",),
    ("crl",),
)


@dataclass(frozen=True)
class PortfolioConfig:
    id: int | None
    name: str
    description: str = ""
    notes: str = ""
    active: bool = True
    display_order: int = 0


@dataclass(frozen=True)
class PortfolioMember:
    id: int | None
    portfolio_id: int
    asset_id: int | None
    external_name: str
    nif: str = ""
    sub_account: str = ""
    active: bool = True
    display_order: int = 0
    mapping_method: str = MAPPING_METHOD_UNMAPPED
    mapping_status: str = "mapping_pending"
    mapping_confidence: float = 0.0
    notes: str = ""


@dataclass(frozen=True)
class AssetAlias:
    id: int | None
    asset_id: int
    alias_name: str
    normalized_alias: str
    source: str = "manual"
    active: bool = True
    notes: str = ""


@dataclass(frozen=True)
class MappingCandidate:
    asset_id: int
    asset_name: str
    nif: str
    matched_alias: str | None
    method: str
    score: float
    confidence: str
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    conflict: bool = False


@dataclass(frozen=True)
class MappingDecision:
    asset_id: int | None
    method: str
    score: float
    confidence: str
    status: str
    candidates: tuple[MappingCandidate, ...]
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    auto_mappable: bool = False


@dataclass(frozen=True)
class MappingConflict:
    code: str
    message: str
    asset_ids: tuple[int, ...] = ()
    portfolio_asset_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PortfolioImportRow:
    row_number: int
    portfolio: str
    sub_account: str = ""
    external_name: str = ""
    nif: str = ""
    asset_name: str = ""
    asset_id: int | None = None
    alias: str = ""
    notes: str = ""
    active: bool = True
    action: str = "pending"
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    decision: MappingDecision | None = None


@dataclass(frozen=True)
class PortfolioImportPreview:
    rows: tuple[PortfolioImportRow, ...]
    rows_total: int
    rows_valid: int
    rows_pending: int
    rows_conflict: int


def normalize_nif(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def normalize_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    text = unicodedata.normalize("NFKD", raw)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " e ")
    text = re.sub(r"[-_/.,;:()\[\]{}]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    tokens = text.split()
    tokens = _strip_company_suffix(tokens)
    return " ".join(tokens)


def _strip_company_suffix(tokens: list[str]) -> list[str]:
    stripped = list(tokens)
    changed = True
    while changed and stripped:
        changed = False
        for suffix in COMPANY_SUFFIXES:
            if len(stripped) >= len(suffix) and tuple(stripped[-len(suffix) :]) == suffix:
                stripped = stripped[: -len(suffix)]
                changed = True
                break
    return stripped


def tokenize_name(value: Any) -> tuple[str, ...]:
    return tuple(token for token in normalize_name(value).split() if token and token != "e")


def compare_names(left: Any, right: Any, *, alias_bonus: bool = False) -> float:
    left_norm = normalize_name(left)
    right_norm = normalize_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(tokenize_name(left_norm))
    right_tokens = set(tokenize_name(right_norm))
    if not left_tokens or not right_tokens:
        return 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm).ratio()
    overlap = len(left_tokens & right_tokens)
    fuzzy_overlap = sum(
        1
        for left_token in left_tokens
        if max((SequenceMatcher(None, left_token, right_token).ratio() for right_token in right_tokens), default=0.0) >= 0.86
    )
    union = len(left_tokens | right_tokens)
    token_similarity = max(overlap / union, fuzzy_overlap / max(len(left_tokens), len(right_tokens)))
    coverage = min(fuzzy_overlap / len(left_tokens), fuzzy_overlap / len(right_tokens))
    divergent = max(len(left_tokens), len(right_tokens)) - fuzzy_overlap
    penalty = min(divergent * 0.08, 0.30)
    score = sequence * 0.40 + token_similarity * 0.30 + coverage * 0.30 - penalty
    if alias_bonus:
        score += 0.03
    return max(0.0, min(score, 1.0))


def confidence_label(score: float) -> str:
    if score >= STRONG_SUGGESTION_THRESHOLD:
        return "strong"
    if score >= POSSIBLE_SUGGESTION_THRESHOLD:
        return "possible"
    return "low"


def validate_portfolio_name(name: str) -> str:
    clean = re.sub(r"\s+", " ", str(name or "").strip())
    if not clean:
        raise ValueError("portfolio_name_required")
    if len(clean) > 120:
        raise ValueError("portfolio_name_too_long")
    return clean


def validate_alias(alias_name: str) -> tuple[str, str]:
    clean = re.sub(r"\s+", " ", str(alias_name or "").strip())
    if not clean:
        raise ValueError("alias_required")
    if len(clean) > 160:
        raise ValueError("alias_too_long")
    normalized = normalize_name(clean)
    if not normalized:
        raise ValueError("alias_invalid")
    return clean, normalized


def decide_mapping(
    *,
    external_name: str,
    nif: str,
    assets: tuple[dict[str, Any], ...],
    aliases: tuple[dict[str, Any], ...],
) -> MappingDecision:
    normalized_nif = normalize_nif(nif)
    normalized_name = normalize_name(external_name)
    candidates: list[MappingCandidate] = []
    warnings: list[str] = []

    if normalized_nif:
        nif_assets = [asset for asset in assets if normalize_nif(asset.get("nif")) == normalized_nif]
        if len(nif_assets) == 1:
            asset = nif_assets[0]
            candidates.append(_candidate(asset, None, MAPPING_METHOD_NIF_EXACT, 1.0, ("nif_exact",)))
        elif len(nif_assets) > 1:
            warnings.append("nif_conflict")
            for asset in nif_assets:
                candidates.append(_candidate(asset, None, MAPPING_METHOD_CONFLICT, 1.0, ("nif_duplicate",), conflict=True))

    if normalized_name:
        alias_matches = [alias for alias in aliases if alias.get("active", True) and alias.get("normalized_alias") == normalized_name]
        alias_asset_ids = {int(alias["asset_id"]) for alias in alias_matches}
        if len(alias_asset_ids) == 1:
            alias = alias_matches[0]
            asset = _asset_by_id(assets, int(alias["asset_id"]))
            if asset:
                candidates.append(_candidate(asset, str(alias.get("alias_name") or ""), MAPPING_METHOD_ALIAS_EXACT, 0.98, ("alias_exact",)))
        elif len(alias_asset_ids) > 1:
            warnings.append("alias_conflict")
            for alias in alias_matches:
                asset = _asset_by_id(assets, int(alias["asset_id"]))
                if asset:
                    candidates.append(_candidate(asset, str(alias.get("alias_name") or ""), MAPPING_METHOD_CONFLICT, 0.98, ("alias_duplicate",), conflict=True))

        name_matches = [asset for asset in assets if normalize_name(asset.get("project_name")) == normalized_name]
        if len(name_matches) == 1:
            candidates.append(_candidate(name_matches[0], None, MAPPING_METHOD_NAME_EXACT, 0.95, ("name_exact",)))
        elif len(name_matches) > 1:
            warnings.append("name_conflict")
            for asset in name_matches:
                candidates.append(_candidate(asset, None, MAPPING_METHOD_CONFLICT, 0.95, ("name_duplicate",), conflict=True))

    best_by_asset = _best_candidates(candidates)
    if best_by_asset:
        exact = _resolve_exact(best_by_asset, warnings)
        if exact:
            return exact

    fuzzy = _fuzzy_candidates(normalized_name, assets, aliases)
    best = _best_candidates([*best_by_asset, *fuzzy])
    if not best:
        return MappingDecision(None, MAPPING_METHOD_UNMAPPED, 0.0, "low", "mapping_pending", (), warnings=tuple(sorted(set(warnings))))

    ordered = sorted(best, key=lambda item: item.score, reverse=True)
    top = ordered[0]
    if top.conflict:
        return MappingDecision(None, MAPPING_METHOD_CONFLICT, top.score, "conflict", "mapping_conflict", tuple(ordered), warnings=tuple(sorted(set(warnings))), auto_mappable=False)
    if len(ordered) > 1 and top.score - ordered[1].score < AMBIGUOUS_DELTA:
        warnings.append("close_candidates")
        return MappingDecision(None, MAPPING_METHOD_CONFLICT, top.score, "conflict", "mapping_conflict", tuple(ordered[:5]), warnings=tuple(sorted(set(warnings))), auto_mappable=False)
    if top.score >= STRONG_SUGGESTION_THRESHOLD:
        return MappingDecision(top.asset_id, top.method, top.score, top.confidence, "mapping_suggested", tuple(ordered[:5]), warnings=tuple(sorted(set(warnings))), auto_mappable=False)
    if top.score >= POSSIBLE_SUGGESTION_THRESHOLD:
        return MappingDecision(None, top.method, top.score, top.confidence, "mapping_pending", tuple(ordered[:5]), warnings=tuple(sorted(set(warnings))), auto_mappable=False)
    return MappingDecision(None, MAPPING_METHOD_UNMAPPED, 0.0, "low", "mapping_pending", tuple(ordered[:5]), warnings=tuple(sorted(set(warnings))), auto_mappable=False)


def _resolve_exact(candidates: list[MappingCandidate], warnings: list[str]) -> MappingDecision | None:
    exact = [item for item in candidates if item.method in SAFE_AUTO_METHODS]
    if not exact:
        return None
    asset_ids = {item.asset_id for item in exact}
    if len(asset_ids) > 1:
        warnings.append("identifier_disagreement")
        return MappingDecision(None, MAPPING_METHOD_CONFLICT, max(item.score for item in exact), "conflict", "mapping_conflict", tuple(exact), warnings=tuple(sorted(set(warnings))), auto_mappable=False)
    top = max(exact, key=lambda item: item.score)
    return MappingDecision(top.asset_id, top.method, top.score, top.confidence, "mapped", tuple(exact), reasons=top.reasons, warnings=tuple(sorted(set(warnings))), auto_mappable=True)


def _fuzzy_candidates(normalized_name: str, assets: tuple[dict[str, Any], ...], aliases: tuple[dict[str, Any], ...]) -> list[MappingCandidate]:
    if not normalized_name:
        return []
    candidates: list[MappingCandidate] = []
    for asset in assets:
        score = compare_names(normalized_name, asset.get("project_name"))
        if score >= POSSIBLE_SUGGESTION_THRESHOLD:
            candidates.append(_candidate(asset, None, MAPPING_METHOD_NAME_FUZZY, score, ("name_fuzzy",)))
    for alias in aliases:
        if not alias.get("active", True):
            continue
        asset = _asset_by_id(assets, int(alias["asset_id"]))
        if not asset:
            continue
        score = compare_names(normalized_name, alias.get("alias_name"), alias_bonus=True)
        if score >= POSSIBLE_SUGGESTION_THRESHOLD:
            candidates.append(_candidate(asset, str(alias.get("alias_name") or ""), MAPPING_METHOD_ALIAS_FUZZY, score, ("alias_fuzzy",)))
    return candidates


def _best_candidates(candidates: list[MappingCandidate]) -> list[MappingCandidate]:
    best: dict[int, MappingCandidate] = {}
    for candidate in candidates:
        current = best.get(candidate.asset_id)
        if current is None or candidate.score > current.score:
            best[candidate.asset_id] = candidate
    return list(best.values())


def _asset_by_id(assets: tuple[dict[str, Any], ...], asset_id: int) -> dict[str, Any] | None:
    return next((asset for asset in assets if int(asset["id"]) == asset_id), None)


def _candidate(
    asset: dict[str, Any],
    alias: str | None,
    method: str,
    score: float,
    reasons: tuple[str, ...],
    *,
    conflict: bool = False,
) -> MappingCandidate:
    return MappingCandidate(
        asset_id=int(asset["id"]),
        asset_name=str(asset.get("project_name") or ""),
        nif=normalize_nif(asset.get("nif")),
        matched_alias=alias,
        method=method,
        score=round(score, 4),
        confidence="conflict" if conflict else confidence_label(score),
        reasons=reasons,
        warnings=("conflict",) if conflict else (),
        conflict=conflict,
    )
