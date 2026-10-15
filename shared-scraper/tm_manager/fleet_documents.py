"""Portfolio-authorized access to server-accepted fleet artifacts."""
import mimetypes
import io
from flask import abort, send_file
import config
import db
from fleet_storage import artifact_bytes, StorageUnavailable


def stored_response(conn, artifact):
    options = {k: getattr(config, 'GOOGLE_DRIVE_' + k.upper(), '') for k in ('client_id','client_secret','refresh_token','root_folder_id')}
    try:
        content = artifact_bytes(conn, artifact, config.CONTROL_ARTIFACT_DIR, options)
    except StorageUnavailable:
        abort(503, description='Accepted document is temporarily unavailable.')
    response = send_file(io.BytesIO(content), download_name=artifact['filename'],
                         mimetype=mimetypes.guess_type(artifact['filename'])[0] or 'application/octet-stream')
    response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def serve_source(conn, app_no, source_key):
    row = conn.execute('SELECT * FROM fleet_control.document_ledger WHERE app_no=%s AND source_key=%s AND sha256 IS NOT NULL',
                       (app_no, source_key)).fetchone()
    if not row:
        abort(404)
    return stored_response(conn, row)


def serve(conn, app_no, kind, filename=None):
    if not config.FLEET_CONTROL_ENABLED:
        return None
    artifact = next((a for a in db.fleet_artifacts(conn, app_no)
                     if a['kind'] == kind and (filename is None or a['filename'] == filename)), None)
    if not artifact:
        return None
    return stored_response(conn, artifact)
