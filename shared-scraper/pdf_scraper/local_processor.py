"""Pure extraction entrypoint: no Drive client or database imports."""


def extract_local(content):
    import os
    os.environ['SCRAPER_LOAD_LEGACY_ENV'] = '0'
    os.environ['SCRAPER_EXTERNAL_VPN_CONTROL'] = '1'
    from ipindia_scraper import extract_document_data, extract_logo_image
    fields = extract_document_data(content)
    try:
        logo = extract_logo_image(content) if fields and fields.get('form_type') in ('TM-A', 'TM-1') else None
    except Exception:
        logo = None
    return fields, logo
