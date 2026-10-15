"""Existing administrator session -> local coordinator; token stays server-side."""
import os
from urllib.parse import quote, urlsplit

import requests
from flask import Blueprint, Response, g, jsonify, request

bp = Blueprint('control_admin_proxy', __name__)


@bp.route('/api/control/v1/admin/<path:path>', methods=['GET', 'POST', 'PATCH', 'DELETE'])
def forward(path):
    if not getattr(g, 'user', None) or not getattr(g, 'is_admin', False):
        return jsonify(error='Administrator sign-in required.'), 403
    if request.method != 'GET':
        origin = request.headers.get('Origin')
        if request.headers.get('X-Fleet-Request') != '1' or (origin and urlsplit(origin).netloc != request.host):
            return jsonify(error='Invalid administration request origin.'), 403
    if any(part in ('', '.', '..') for part in path.split('/')):
        return jsonify(error='Invalid administration path.'), 400
    base = os.environ.get('TM_CONTROL_API_BASE', 'http://127.0.0.1:5010')
    url = urlsplit(base)
    token = os.environ.get('TM_CONTROL_ADMIN_TOKEN', '')
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password or url.path or url.query or url.fragment or not token:
        return jsonify(error='Coordinator connection is not configured.'), 503
    try:
        with requests.Session() as client:
            client.trust_env = False
            with client.request(request.method, base + '/api/control/v1/admin/' + quote(path, safe='/'),
                                params=list(request.args.items(multi=True)), data=request.get_data(),
                                headers={'Authorization': 'Bearer ' + token,
                                         'Content-Type': request.content_type or 'application/json'},
                                timeout=(3, 30), allow_redirects=False, stream=True) as reply:
                chunks, size = [], 0
                for chunk in reply.iter_content(65536):
                    size += len(chunk)
                    if size > 34 * 1024 * 1024:
                        return jsonify(error='Coordinator response exceeds the download limit.'), 502
                    chunks.append(chunk)
                headers = {key: reply.headers[key] for key in ('Content-Type', 'Content-Disposition', 'Content-Security-Policy') if key in reply.headers}
                headers['X-Content-Type-Options'] = 'nosniff'
                headers['Cache-Control'] = 'no-store'
                return Response(b''.join(chunks), status=reply.status_code, headers=headers)
    except requests.RequestException:
        return jsonify(error='Coordinator unavailable; retry shortly.'), 503
