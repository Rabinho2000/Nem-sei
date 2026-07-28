(() => {
  "use strict";

  const forms = document.querySelectorAll("#reports-generate-form, #reports-automation-form");

  function selectedReportType(form) {
    return form.querySelector("input[name='report_type']:checked")?.value || "individual";
  }

  function replaceReportScopeInUrl(form) {
    if (form.id !== "reports-generate-form") return;
    const reportType = selectedReportType(form);
    const url = new URL(window.location.href);
    url.searchParams.set("report_type", reportType);
    if (reportType === "individual") {
      const assetId = form.querySelector("select[name='asset_id']")?.value || "";
      if (assetId) url.searchParams.set("asset_id", assetId);
      else url.searchParams.delete("asset_id");
      url.searchParams.delete("portfolio_id");
      url.searchParams.delete("profile_id");
    } else {
      const portfolioId = form.querySelector("select[name='portfolio_id']")?.value || "";
      if (portfolioId) url.searchParams.set("portfolio_id", portfolioId);
      else url.searchParams.delete("portfolio_id");
      url.searchParams.delete("asset_id");
    }
    window.history.replaceState(window.history.state, "", url);
  }

  function syncTemplateOptions(form, reportType) {
    const select = form.querySelector("select[name='template_id']");
    if (!select) return;
    const options = Array.from(select.options);
    options.forEach((option) => {
      const matches = !option.dataset.templateType || option.dataset.templateType === reportType;
      option.hidden = !matches;
      option.disabled = !matches;
    });
    if (select.selectedOptions[0]?.disabled) {
      const first = options.find((option) => !option.disabled);
      if (first) first.selected = true;
    }
  }

  function syncReportScope(form) {
    const reportType = selectedReportType(form);
    form.querySelectorAll("[data-report-scope]").forEach((element) => {
      const matches = element.dataset.reportScope === reportType;
      element.hidden = !matches;
      element.querySelectorAll("input, select, textarea").forEach((control) => {
        control.disabled = !matches;
      });
    });
    const asset = form.querySelector("select[name='asset_id']");
    const portfolio = form.querySelector("select[name='portfolio_id']");
    if (asset) asset.required = reportType === "individual";
    if (portfolio) portfolio.required = reportType === "portfolio";
    syncTemplateOptions(form, reportType);
    updateSummary(form);
  }

  function syncPeriodFields(form) {
    const periodType = form.querySelector("select[name='period_type']")?.value || "monthly";
    form.querySelectorAll("[data-period-field]").forEach((field) => {
      const matches = field.dataset.periodField.split(/\s+/).includes(periodType);
      field.hidden = !matches;
      field.querySelectorAll("input, select").forEach((control) => {
        control.disabled = !matches;
      });
    });
    updateSummary(form);
  }

  function syncBilling(form) {
    const asset = form.querySelector("select[name='asset_id']");
    const billingMode = form.querySelector("select[name='billing_mode']");
    const reportModel = asset?.selectedOptions[0]?.dataset.reportType || "";
    const isEsco = reportModel === "esco";
    const isFixed = billingMode?.value === "fixed_monthly_fee";
    const modelLabel = form.querySelector("#detected-report-model");
    if (modelLabel) {
      modelLabel.textContent = reportModel
        ? `Modelo detetado: ${reportModel.toUpperCase()}`
        : "O modelo ESCO/EPC é detetado pela instalação.";
    }
    const visibility = [
      ["#solcor-price-field", isEsco && !isFixed],
      ["#fixed-monthly-fee-field", isEsco && isFixed],
      ["#charge-total-production-field", isEsco && !isFixed],
    ];
    visibility.forEach(([selector, visible]) => {
      const field = form.querySelector(selector);
      if (field) field.hidden = !visible;
    });
    const base = form.querySelector("#billing-energy-base");
    const charge = form.querySelector("input[name='charge_total_production']");
    if (base && charge) base.value = charge.checked ? "total_production" : "self_consumption";
    const details = form.querySelector("#reports-calculation");
    if (details && form.querySelector("#billing-values-source")?.value === "manual") details.open = true;
    updateSummary(form);
  }

  function optionLabel(select) {
    return select?.selectedOptions[0]?.textContent.trim() || "Por selecionar";
  }

  function updateSummary(form) {
    if (form.id !== "reports-generate-form") return;
    const reportType = selectedReportType(form);
    const periodType = form.querySelector("select[name='period_type']")?.value || "monthly";
    let period = form.querySelector("input[name='report_month']")?.value || "Por selecionar";
    if (periodType === "quarterly") {
      period = `T${form.querySelector("select[name='report_quarter']")?.value || ""} ${form.querySelector("input[name='report_year']")?.value || ""}`;
    } else if (periodType === "semiannual") {
      period = `S${form.querySelector("select[name='report_semester']")?.value || ""} ${form.querySelector("input[name='report_year']")?.value || ""}`;
    } else if (periodType === "annual") {
      period = form.querySelector("input[name='report_year']")?.value || "Por selecionar";
    }
    const scopeSelect = form.querySelector(
      reportType === "individual" ? "select[name='asset_id']" : "select[name='portfolio_id']"
    );
    const values = {
      type: reportType === "individual" ? "Individual" : "Portefólio",
      scope: optionLabel(scopeSelect),
      period,
      template: optionLabel(form.querySelector("select[name='template_id']")),
      source: form.querySelector("#billing-values-source")?.value === "manual" ? "Valores manuais" : "Configuração guardada",
      billing: form.querySelector("#billing-mode")?.value === "fixed_monthly_fee" ? "Mensalidade fixa" : "Tarifa por energia",
      availability: form.querySelector("input[name='include_availability']")?.checked ? "Incluída" : "Não incluída",
    };
    Object.entries(values).forEach(([key, value]) => {
      const target = document.querySelector(`[data-summary='${key}']`);
      if (target) target.textContent = value;
    });
  }

  forms.forEach((form) => {
    form.querySelectorAll("input[name='report_type']").forEach((radio) => {
      radio.addEventListener("change", () => {
        syncReportScope(form);
        replaceReportScopeInUrl(form);
      });
    });
    form.querySelector("select[name='asset_id']")?.addEventListener("change", () => replaceReportScopeInUrl(form));
    form.querySelector("select[name='portfolio_id']")?.addEventListener("change", () => replaceReportScopeInUrl(form));
    form.querySelector("select[name='period_type']")?.addEventListener("change", () => syncPeriodFields(form));
    form.querySelectorAll("select, input").forEach((control) => {
      control.addEventListener("change", () => {
        syncBilling(form);
        updateSummary(form);
      });
    });
    syncReportScope(form);
    syncPeriodFields(form);
    syncBilling(form);
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented || form.dataset.submitting === "true") {
        if (form.dataset.submitting === "true") event.preventDefault();
        return;
      }
      form.dataset.submitting = "true";
      window.setTimeout(() => {
        form.querySelectorAll("button[type='submit']").forEach((button) => {
          button.disabled = true;
          if (button.dataset.submitLabel) button.textContent = "A processar…";
        });
      }, 0);
    });
  });
})();
