/**
 * Eventyay Business - Tier & Entitlements Form Interactions
 * Standard ES Module (No jQuery, No Inline Scripts)
 */

export function initTierForm() {
    const dataScript = document.getElementById('capabilities-data');
    let capabilities = {};
    if (dataScript) {
        try {
            capabilities = JSON.parse(dataScript.textContent);
        } catch (e) {
            console.error('Failed to parse capabilities data:', e);
        }
    }

    function setupEntitlementRow(row) {
        if (!row || row.dataset.tierInitDone) return;
        row.dataset.tierInitDone = 'true';

        const capSelect = row.querySelector('.entitlement-capability-select, select[name$="-capability"]');
        if (!capSelect) return;

        const valueCol = row.querySelector('[data-col-value]');
        const unitCol = row.querySelector('[data-col-unit]');
        const overageCol = row.querySelector('[data-col-overage]');
        const overageToggle = row.querySelector('.entitlement-overage-toggle, input[name$="-overage_allowed"]');
        const overageFields = row.querySelector('[data-overage-fields]');
        const unitInput = row.querySelector('.entitlement-unit-input, input[name$="-unit"]');

        // Description helper under capability select
        let descEl = row.querySelector('.capability-description-text');
        if (!descEl && capSelect.parentElement) {
            descEl = document.createElement('div');
            descEl.className = 'capability-description-text text-muted small mt-1';
            descEl.style.fontSize = '0.85em';
            descEl.style.lineHeight = '1.3';
            descEl.style.marginTop = '4px';
            capSelect.parentElement.appendChild(descEl);
        }

        function updateRowState() {
            const capName = capSelect.value;
            const cap = capabilities[capName];

            // 1. Update description
            if (descEl) {
                if (cap && cap.description) {
                    descEl.textContent = cap.description;
                    descEl.style.display = 'block';
                } else {
                    descEl.textContent = '';
                    descEl.style.display = 'none';
                }
            }

            // Find current value input or select
            const currentValueEl = row.querySelector('input[name$="-value"], select[name$="-value"]');
            if (!currentValueEl) return;

            const nameAttr = currentValueEl.name;
            const idAttr = currentValueEl.id;
            const currentVal = (currentValueEl.value || '').trim();

            if (cap && cap.value_type === 'boolean') {
                // Feature toggle (Boolean)
                if (currentValueEl.tagName.toLowerCase() !== 'select') {
                    const select = document.createElement('select');
                    select.name = nameAttr;
                    select.id = idAttr;
                    select.className = 'form-control entitlement-value-input';

                    const optTrue = document.createElement('option');
                    optTrue.value = 'true';
                    optTrue.textContent = '✓ Included (Enabled)';

                    const optFalse = document.createElement('option');
                    optFalse.value = 'false';
                    optFalse.textContent = '✕ Not Included (Disabled)';

                    select.appendChild(optTrue);
                    select.appendChild(optFalse);

                    const isTrue = currentVal.toLowerCase() in { 'true': 1, '1': 1, 'yes': 1, 'included': 1, 'enabled': 1 };
                    select.value = isTrue ? 'true' : (currentVal ? 'false' : 'true');

                    currentValueEl.parentNode.replaceChild(select, currentValueEl);
                }

                // Hide unit for boolean capabilities
                if (unitCol) {
                    unitCol.style.opacity = '0.35';
                    unitCol.style.pointerEvents = 'none';
                }
                if (unitInput) {
                    unitInput.value = '';
                    unitInput.placeholder = 'N/A (Feature)';
                }

                // Hide overage for booleans
                if (overageCol) {
                    overageCol.style.display = 'none';
                }
                if (overageToggle) {
                    overageToggle.checked = false;
                }
                if (overageFields) {
                    overageFields.style.display = 'none';
                }
            } else {
                // Numeric quota, percentage, or text
                if (currentValueEl.tagName.toLowerCase() === 'select') {
                    const input = document.createElement('input');
                    input.type = 'text';
                    input.name = nameAttr;
                    input.id = idAttr;
                    input.className = 'form-control entitlement-value-input';
                    input.value = currentVal === 'false' ? '0' : (currentVal === 'true' ? '1' : currentVal);
                    currentValueEl.parentNode.replaceChild(input, currentValueEl);
                }

                const activeInput = row.querySelector('input[name$="-value"]');
                if (activeInput) {
                    if (cap && (cap.value_type === 'integer' || cap.value_type === 'decimal' || cap.value_type === 'money')) {
                        activeInput.type = 'number';
                        activeInput.step = cap.value_type === 'integer' ? '1' : 'any';
                        activeInput.placeholder = cap.value_type === 'integer' ? 'e.g. 10' : 'e.g. 2.50';
                    } else {
                        activeInput.type = 'text';
                        activeInput.placeholder = 'Value / Allowance';
                    }
                }

                // Show & auto-populate unit
                if (unitCol) {
                    unitCol.style.opacity = '1';
                    unitCol.style.pointerEvents = 'auto';
                }
                if (unitInput) {
                    unitInput.placeholder = (cap && cap.unit) ? cap.unit : 'Unit (e.g. %)';
                    if (!unitInput.value && cap && cap.unit) {
                        unitInput.value = cap.unit;
                    }
                }

                // Show overage section for quotas
                if (overageCol) {
                    overageCol.style.display = '';
                }
                updateOverageVisibility();
            }
        }

        function updateOverageVisibility() {
            if (!overageFields) return;
            if (overageToggle && overageToggle.checked) {
                overageFields.style.display = 'block';
            } else {
                overageFields.style.display = 'none';
            }
        }

        capSelect.addEventListener('change', updateRowState);
        if (overageToggle) {
            overageToggle.addEventListener('change', updateOverageVisibility);
        }

        updateRowState();
    }

    // Initialize existing entitlement rows
    const entitlementFormset = document.querySelector('[data-formset-prefix="entitlements"]');
    if (entitlementFormset) {
        const bindEntitlementRows = () => {
            entitlementFormset.querySelectorAll('[data-formset-form]').forEach(setupEntitlementRow);
        };
        bindEntitlementRows();

        const body = entitlementFormset.querySelector('[data-formset-body]');
        if (body) {
            const observer = new MutationObserver(bindEntitlementRows);
            observer.observe(body, { childList: true });
        }
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initTierForm);
} else {
    initTierForm();
}
