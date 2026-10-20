async function fetchQuote(url, params) {
    const response = await fetch(`${url}?${params}`, {
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
    if (!response.ok) {
        throw new Error(`Voucher quote failed with status ${response.status}`);
    }
    return response.json();
}

function showMessage(element, text, valid) {
    element.textContent = text;
    element.classList.toggle('text-success', valid);
    element.classList.toggle('text-danger', !valid);
}

export function initPlanVoucher() {
    const container = document.getElementById('plan-voucher');
    if (!container) return;

    const input = document.getElementById('plan-voucher-code');
    const button = document.getElementById('plan-voucher-apply');
    const message = document.getElementById('plan-voucher-message');
    const forms = Array.from(document.querySelectorAll('form[data-tier-price-id]'));

    function reset() {
        forms.forEach((form) => {
            form.querySelector('[data-voucher-code]').value = '';
            form.querySelector('[data-voucher-price]').style.textDecoration = '';
            const result = form.querySelector('[data-voucher-result]');
            result.textContent = '';
            result.hidden = true;
        });
    }

    async function apply() {
        const code = input.value.trim();
        reset();
        if (!code) {
            message.textContent = '';
            return;
        }
        const params = new URLSearchParams({ code });
        forms.forEach((form) => params.append('tier_price', form.dataset.tierPriceId));

        let data;
        try {
            data = await fetchQuote(container.dataset.quoteUrl, params);
        } catch (error) {
            showMessage(message, container.dataset.errorMessage, false);
            return;
        }
        showMessage(message, data.message, data.valid);
        forms.forEach((form) => {
            const item = data.items[`tier_price-${form.dataset.tierPriceId}`];
            if (!item || !item.valid) return;
            form.querySelector('[data-voucher-code]').value = code;
            form.querySelector('[data-voucher-price]').style.textDecoration = 'line-through';
            const result = form.querySelector('[data-voucher-result]');
            result.textContent = `${item.total} ${item.currency} · ${item.message}`;
            result.hidden = false;
        });
    }

    button.addEventListener('click', apply);
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            apply();
        }
    });
}

export function initAddonVoucher() {
    const pricing = document.getElementById('addon-pricing-display');
    const input = document.getElementById('id_voucher_code');
    const result = document.getElementById('addon-voucher-result');
    if (!pricing || !input || !result || !pricing.dataset.quoteUrl) return;

    const quantityInput = document.getElementById('id_quantity');
    const eventSelect = document.getElementById('id_event');

    async function update() {
        const code = input.value.trim();
        if (!code) {
            result.textContent = '';
            result.hidden = true;
            return;
        }
        const params = new URLSearchParams({ code, addon: pricing.dataset.addonId });
        if (quantityInput) params.set('quantity', quantityInput.value || '1');
        if (eventSelect && eventSelect.value) params.set('event', eventSelect.value);

        let data;
        try {
            data = await fetchQuote(pricing.dataset.quoteUrl, params);
        } catch (error) {
            showMessage(result, pricing.dataset.errorMessage, false);
            result.hidden = false;
            return;
        }
        const item = data.items[`addon-${pricing.dataset.addonId}`];
        if (item && item.valid) {
            showMessage(
                result,
                `${pricing.dataset.discountLabel} ${item.total} ${item.currency} (−${item.discount} ${item.currency}). ${item.message}`,
                true,
            );
        } else {
            showMessage(result, item ? item.message : data.message, false);
        }
        result.hidden = false;
    }

    input.addEventListener('change', update);
    if (quantityInput) quantityInput.addEventListener('change', update);
    if (eventSelect) eventSelect.addEventListener('change', update);
    if (input.value.trim()) update();
}

function init() {
    initPlanVoucher();
    initAddonVoucher();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
