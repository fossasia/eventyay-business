/**
 * Eventyay Business - Tier & Addon Form Interactions
 * Standard ES Module (No jQuery, No Inline Scripts)
 */

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

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
            descEl = document.createElement('p');
            descEl.className = 'help-block capability-description-text';
            descEl.style.fontSize = '12px';
            descEl.style.lineHeight = '1.4';
            descEl.style.marginTop = '5px';
            descEl.style.marginBottom = '0';
            descEl.style.color = '#737373';
            capSelect.parentElement.appendChild(descEl);
        }

        function updateRowState() {
            const capName = capSelect.value;
            const cap = capabilities[capName];

            // 1. Update description
            if (descEl) {
                if (cap && cap.description) {
                    descEl.innerHTML = '<i class="fa fa-info-circle text-info" style="margin-right: 4px;"></i>' + escapeHtml(cap.description);
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

                    const optEmpty = document.createElement('option');
                    optEmpty.value = '';
                    optEmpty.textContent = '---------';

                    const optTrue = document.createElement('option');
                    optTrue.value = 'true';
                    optTrue.textContent = '✓ Included (Enabled)';

                    const optFalse = document.createElement('option');
                    optFalse.value = 'false';
                    optFalse.textContent = '✕ Not Included (Disabled)';

                    select.appendChild(optEmpty);
                    select.appendChild(optTrue);
                    select.appendChild(optFalse);

                    if (!currentVal) {
                        select.value = '';
                    } else {
                        const isTrue = currentVal.toLowerCase() in { 'true': 1, '1': 1, 'yes': 1, 'included': 1, 'enabled': 1 };
                        select.value = isTrue ? 'true' : 'false';
                    }

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

                // Hide overage for booleans without removing column from grid layout (prevents column shifting)
                if (overageCol) {
                    overageCol.style.visibility = 'hidden';
                    overageCol.style.pointerEvents = 'none';
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

                // Show overage column for quotas
                if (overageCol) {
                    overageCol.style.visibility = 'visible';
                    overageCol.style.pointerEvents = 'auto';
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

export function initAddonForm() {
    const dataScript = document.getElementById('capabilities-data');
    let capabilities = {};
    if (dataScript) {
        try {
            capabilities = JSON.parse(dataScript.textContent);
        } catch (e) {
            console.error('Failed to parse capabilities data:', e);
        }
    }

    const capSelect = document.querySelector('.addon-capability-select, select[name="capability"]');
    if (!capSelect) return;

    // Helper to find or create description element
    let descEl = capSelect.parentElement.querySelector('.capability-description-text');
    if (!descEl) {
        descEl = document.createElement('p');
        descEl.className = 'help-block capability-description-text';
        descEl.style.fontSize = '12px';
        descEl.style.lineHeight = '1.4';
        descEl.style.marginTop = '6px';
        descEl.style.marginBottom = '0';
        descEl.style.color = '#737373';
        capSelect.parentElement.appendChild(descEl);
    }

    function updateAddonFields() {
        const capName = capSelect.value;
        const cap = capabilities[capName];

        // 1. Description
        if (cap && cap.description) {
            descEl.innerHTML = '<i class="fa fa-info-circle text-info" style="margin-right: 4px;"></i>' + escapeHtml(cap.description);
            descEl.style.display = 'block';
        } else {
            descEl.textContent = '';
            descEl.style.display = 'none';
        }

        // 2. Value input adaptation
        const valueInput = document.querySelector('.addon-value-input, [name="entitlement_value"]');
        if (!valueInput) return;

        const currentVal = (valueInput.value || '').trim();
        const nameAttr = valueInput.name || 'entitlement_value';
        const idAttr = valueInput.id || 'id_entitlement_value';

        if (cap && cap.value_type === 'boolean') {
            if (valueInput.tagName.toLowerCase() !== 'select') {
                const select = document.createElement('select');
                select.name = nameAttr;
                select.id = idAttr;
                select.className = 'form-control addon-value-input';

                const optEmpty = document.createElement('option');
                optEmpty.value = '';
                optEmpty.textContent = '---------';

                const optTrue = document.createElement('option');
                optTrue.value = 'true';
                optTrue.textContent = '✓ Included (Enabled)';

                const optFalse = document.createElement('option');
                optFalse.value = 'false';
                optFalse.textContent = '✕ Not Included (Disabled)';

                select.appendChild(optEmpty);
                select.appendChild(optTrue);
                select.appendChild(optFalse);

                if (!currentVal) {
                    select.value = '';
                } else {
                    const isTrue = currentVal.toLowerCase() in { 'true': 1, '1': 1, 'yes': 1, 'included': 1, 'enabled': 1 };
                    select.value = isTrue ? 'true' : 'false';
                }

                valueInput.parentNode.replaceChild(select, valueInput);
            }
        } else {
            if (valueInput.tagName.toLowerCase() === 'select') {
                const input = document.createElement('input');
                input.type = 'text';
                input.name = nameAttr;
                input.id = idAttr;
                input.className = 'form-control addon-value-input';
                input.value = currentVal === 'false' ? '0' : (currentVal === 'true' ? '1' : currentVal);
                valueInput.parentNode.replaceChild(input, valueInput);
            }
            const activeInput = document.querySelector('.addon-value-input, [name="entitlement_value"]');
            if (activeInput) {
                if (cap && (cap.value_type === 'integer' || cap.value_type === 'decimal' || cap.value_type === 'money')) {
                    activeInput.type = 'number';
                    activeInput.step = cap.value_type === 'integer' ? '1' : 'any';
                    const unitSuffix = cap.unit ? ` (${cap.unit})` : '';
                    activeInput.placeholder = (cap.value_type === 'integer' ? 'e.g. 10' : 'e.g. 2.50') + unitSuffix;
                } else {
                    activeInput.type = 'text';
                    activeInput.placeholder = cap && cap.unit ? `Value in ${cap.unit}` : 'Value / Allowance';
                }
            }
        }
    }

    capSelect.addEventListener('change', updateAddonFields);
    updateAddonFields();
}

// Global click handler fallback for formset delete buttons
document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-formset-delete-button]');
    if (!btn) return;
    const row = btn.closest('[data-formset-form]');
    if (!row) return;
    const delInput = row.querySelector('input[type="checkbox"][name$="-DELETE"]');
    if (delInput) {
        delInput.checked = true;
    }
    setTimeout(() => {
        if (row.parentElement && getComputedStyle(row).display !== 'none') {
            row.style.display = 'none';
        }
    }, 50);
});

export function initAll() {
    initTierForm();
    initAddonForm();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initAll);
} else {
    initAll();
}
