# Translation workflow

Issues #714 and #715 define a lightweight translation contribution process.

## Adding strings

1. Add the key to `app/translations/en.json` with clear, concise English.
2. Use it from templates as `{{ t('key') }}` or from JavaScript through
   `window.AICA_I18N.t('key')`.
3. Do not concatenate translated fragments; provide a full sentence per key.

## Translating

1. Copy the key into `app/translations/<locale>.json`.
2. Keep placeholders (`{name}`) and HTML-free text intact.
3. Run `python scripts/extract_i18n_strings.py` and the i18n tests.
4. For RTL locales, check the page with `?lang=ar` and confirm `dir="rtl"`.

## Glossary

| English | Arabic | Notes |
| ------- | ------ | ----- |
| workspace | مساحة عمل | The container for imported projects. |
| project | مشروع | An imported repository or archive. |
| prompt | مطالبة | A reusable instruction for the assistant. |
| review | مراجعة | An AI code review. |
| plugin | إضافة | An optional extension. |
| billing | الفواتير | Invoices and usage cost. |
| notification | إشعار | An in-app or channel message. |
| webhook | ويب هوك | An outbound HTTP delivery target. |
| token | رمز | Keep as "رمز" for API tokens; use context when ambiguous. |

## Review checklist

- [ ] Keys are present in every shipped catalog and match the source locale.
- [ ] No user-facing string is hard-coded in a template or JavaScript file.
- [ ] Placeholders and punctuation are preserved.
- [ ] The layout survives longer translations without clipping.
- [ ] RTL pages mirror layout, while code, URLs, and identifiers stay LTR.
- [ ] `python scripts/extract_i18n_strings.py` and `pytest tests/test_i18n.py`
      pass.
