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
    validateClientOutagePeriod(form);
    updateSummary(form);
  }

  function reportPeriodBounds(form) {
    const type = form.querySelector("select[name='period_type']")?.value || "monthly";
    const month = form.querySelector("input[name='report_month']")?.value || "";
    const year = Number(form.querySelector("input[name='report_year']")?.value);
    let startMonth;
    let endMonth;
    if (type === "monthly" && /^\d{4}-\d{2}$/.test(month)) {
      startMonth = Number(month.slice(5, 7));
      return { start: `${month}-01`, end: new Date(Date.UTC(Number(month.slice(0, 4)), startMonth, 0)).toISOString().slice(0, 10) };
    }
    if (!Number.isInteger(year) || year < 2000 || year > 2100) return null;
    if (type === "quarterly") {
      startMonth = (Number(form.querySelector("select[name='report_quarter']")?.value) - 1) * 3 + 1;
      endMonth = startMonth + 2;
    } else if (type === "semiannual") {
      startMonth = Number(form.querySelector("select[name='report_semester']")?.value) === 2 ? 7 : 1;
      endMonth = startMonth + 5;
    } else if (type === "annual") {
      startMonth = 1;
      endMonth = 12;
    } else {
      return null;
    }
    return {
      start: `${year}-${String(startMonth).padStart(2, "0")}-01`,
      end: new Date(Date.UTC(year, endMonth, 0)).toISOString().slice(0, 10),
    };
  }

  function validateClientOutagePeriod(form) {
    const bounds = reportPeriodBounds(form);
    const error = form.querySelector("[data-client-outage-period-error]");
    let invalid = false;
    form.querySelectorAll("input[name='client_outage_date']").forEach((input) => {
      if (bounds) {
        input.min = bounds.start;
        input.max = bounds.end;
      }
      const outside = !input.disabled && input.value && bounds && (input.value < bounds.start || input.value > bounds.end);
      input.setCustomValidity(outside ? "A data de indisponibilidade tem de pertencer ao período do relatório." : "");
      invalid ||= Boolean(outside);
    });
    if (error) {
      error.hidden = !invalid;
      error.textContent = invalid ? "Há dias de indisponibilidade fora do período selecionado. Corrige-os ou remove-os antes de gerar." : "";
    }
    if (invalid) form.querySelector("#reports-calculation")?.setAttribute("open", "");
    return !invalid;
  }

  function syncBilling(form) {
    const asset = form.querySelector("select[name='asset_id']");
    const billingMode = form.querySelector("select[name='billing_mode']");
    const reportModel = asset?.selectedOptions[0]?.dataset.reportType || "";
    const isEsco = reportModel === "esco";
    const isFixed = billingMode?.value === "fixed_monthly_fee";
    const usesHourlyTariff = asset?.selectedOptions[0]?.dataset.hourlyTariff === "true";
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
    const electricityFallback = form.querySelector("#electricity-price-field");
    if (electricityFallback) electricityFallback.hidden = usesHourlyTariff;
    const tariffNotice = form.querySelector("#hourly-tariff-notice");
    if (tariffNotice) tariffNotice.hidden = !usesHourlyTariff;
    const outage = form.querySelector("#client-outage-adjustments");
    if (outage) {
      outage.hidden = !isEsco || isFixed;
      outage.querySelectorAll("input, textarea, button").forEach((control) => { control.disabled = !isEsco || isFixed; });
    }
    validateClientOutagePeriod(form);
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
    // Native constraint validation can stop the submit event before this
    // handler runs, leaving the user without a message or server request.
    // The report endpoint validates every submitted value and returns the
    // actionable Portuguese error, so keep this flow under our control.
    if (form.id === "reports-generate-form") form.noValidate = true;
    const outageRows = form.querySelector("[data-client-outage-rows]");
    const addOutage = form.querySelector("[data-add-client-outage]");
    function addOutageRow() {
      if (!outageRows) return;
      const row = document.createElement("div");
      row.className = "grid three compact-top-space";
      row.innerHTML = '<label>Dia<input required type="date" name="client_outage_date"></label><label>Produção estimada kWh<input required min="0" step="0.01" type="number" name="client_outage_kwh"></label><label>Motivo (opcional)<input maxlength="240" type="text" name="client_outage_reason"><button class="button secondary small" type="button" data-remove-client-outage>Remover</button></label>';
      row.querySelector("[data-remove-client-outage]").addEventListener("click", () => row.remove());
      outageRows.appendChild(row);
      validateClientOutagePeriod(form);
    }
    if (addOutage) addOutage.addEventListener("click", addOutageRow);
    form.querySelectorAll("input[name='report_type']").forEach((radio) => {
      radio.addEventListener("change", () => {
        syncReportScope(form);
        replaceReportScopeInUrl(form);
      });
    });
    form.querySelector("select[name='asset_id']")?.addEventListener("change", () => replaceReportScopeInUrl(form));
    form.querySelector("select[name='portfolio_id']")?.addEventListener("change", () => replaceReportScopeInUrl(form));
    form.querySelector("select[name='period_type']")?.addEventListener("change", () => syncPeriodFields(form));
    form.querySelectorAll("input[name='report_month'], input[name='report_year'], select[name='report_quarter'], select[name='report_semester']").forEach((control) => {
      control.addEventListener("change", () => validateClientOutagePeriod(form));
    });
    form.querySelector("input[name='export_revenue_enabled']")?.addEventListener("change", () => {
      // This is the only commercial value that is overridden for the current
      // run; the remaining prices continue to come from the saved config.
      form.querySelector("#reports-calculation")?.setAttribute("open", "");
    });
    form.querySelectorAll("select, input").forEach((control) => {
      control.addEventListener("change", () => {
        syncBilling(form);
        updateSummary(form);
      });
    });
    syncReportScope(form);
    syncPeriodFields(form);
    syncBilling(form);

    form.addEventListener("submit", async (event) => {
      const submitter = event.submitter;
      if (!submitter?.dataset.submitLabel) return;
      event.preventDefault();
      if (!validateClientOutagePeriod(form)) {
        form.reportValidity();
        return;
      }
      if (form.dataset.submitting === "true") return;

      form.dataset.submitting = "true";
      const buttons = Array.from(form.querySelectorAll("button[type='submit']"));
      const originalButtons = buttons.map((button) => ({
        button,
        disabled: button.disabled,
        label: button.textContent,
      }));
      buttons.forEach((button) => { button.disabled = true; });
      submitter.textContent = "A processar…";

      try {
        // Do not read form.action/form.method as properties: a named control
        // (the "Guardar configuração" button is named "action") can shadow
        // them and turn the URL into "[object HTMLButtonElement]".
        const response = await window.fetch(form.getAttribute("action") || window.location.href, {
          method: (form.getAttribute("method") || "post").toUpperCase(),
          body: new FormData(form),
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const disposition = response.headers.get("content-disposition") || "";
        if (disposition.toLowerCase().includes("attachment")) {
          const filename = disposition.match(/filename\*?=(?:UTF-8''|\")?([^;\"]+)/i)?.[1]
            ?.replace(/^"|"$/g, "") || "relatorio";
          const link = document.createElement("a");
          link.href = URL.createObjectURL(await response.blob());
          link.download = decodeURIComponent(filename);
          link.hidden = true;
          document.body.appendChild(link);
          link.click();
          link.remove();
          window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        } else if (response.redirected) {
          window.location.assign(response.url);
          return;
        } else {
          window.location.reload();
          return;
        }
      } catch (error) {
        window.alert("Não foi possível gerar ou descarregar o relatório. Tenta novamente.");
      } finally {
        delete form.dataset.submitting;
        originalButtons.forEach(({ button, disabled, label }) => {
          button.disabled = disabled;
          button.textContent = label;
        });
      }
    });
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
