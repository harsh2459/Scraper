"""Change only the audited main site's static root and new fleet routes.

TLS, existing API routes and other virtual hosts remain in their original text.
Ambiguous or already customized fleet routes are rejected for review.
"""
import re


def tokens(text):
    return list(re.finditer(r'#[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{};]|[^\s{};#]+', text))


def servers(text):
    items = [t for t in tokens(text) if not t.group().startswith('#')]
    stack, found = [], []
    for index, token in enumerate(items):
        if token.group() == '{':
            start = index - 1
            while start > 0 and items[start-1].group() not in (';', '{', '}'):
                start -= 1
            stack.append((start, index))
        elif token.group() == '}':
            if not stack:
                raise ValueError('Unbalanced Nginx configuration.')
            start, opening = stack.pop()
            if items[start].group() == 'server':
                # Read only directives immediately inside this server block.
                depth, beginning, directives = 0, opening + 1, []
                for j in range(opening + 1, index):
                    value = items[j].group()
                    if value == '{':
                        depth += 1
                    elif value == '}':
                        depth -= 1
                        if depth == 0:
                            beginning = j + 1
                    elif value == ';' and depth == 0:
                        directives.append(items[beginning:j])
                        beginning = j + 1
                found.append((items[opening].end(), token.start(), directives))
    if stack:
        raise ValueError('Unbalanced Nginx configuration.')
    return found


def update(text, frontend):
    if not re.fullmatch(r'/var/www/tmtracker-live/[a-zA-Z0-9._-]+', frontend):
        raise ValueError('Invalid versioned frontend path.')
    matches = []
    for opening, closing, directives in servers(text):
        names = [x.group().strip('"\'') for d in directives if d and d[0].group() == 'server_name' for x in d[1:]]
        tls = any(d and d[0].group() == 'listen' and any(re.search(r'(^|:)443$', x.group()) for x in d[1:]) for d in directives)
        if 'mytmtracker.com' in names and tls:
            matches.append((opening, closing, directives))
    if len(matches) != 1:
        raise ValueError('Expected one main-domain HTTPS server block; no Nginx changes made.')
    opening, closing, directives = matches[0]
    block = text[opening:closing]
    if re.search(r'\blocation\s+[^\n{]*(?:/admin|/api/control)', block):
        raise ValueError('Existing fleet locations need review; no Nginx changes made.')
    roots = [d for d in directives if d and d[0].group() == 'root']
    if len(roots) != 1 or len(roots[0]) != 2 or roots[0][1].group().strip('"\'') != '/opt/tmtracker/tm_manager/frontend/dist':
        raise ValueError('Main site root differs from the audited application; no Nginx changes made.')
    root = roots[0][1]
    routes = '''
    # TM Tracker fleet production routes
    location = /admin { return 308 /admin/; }
    location ^~ /admin/ {
        proxy_pass http://127.0.0.1:5010;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
    location ^~ /api/control/v1/admin/ {
        proxy_pass http://127.0.0.1:5005;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 34m;
        proxy_read_timeout 90s;
    }
    location ^~ /api/control/ {
        proxy_pass http://127.0.0.1:5010;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        client_max_body_size 34m;
        proxy_read_timeout 180s;
    }
'''
    return text[:root.start()] + frontend + text[root.end():closing] + routes + text[closing:]
