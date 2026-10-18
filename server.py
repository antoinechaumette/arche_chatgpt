"""Private, read-only ARCHE bridge. Secrets belong in environment variables."""
import hmac
import html
import io
import json
import os
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from pypdf import PdfReader

BASE = 'https://arche.univ-lorraine.fr'
LIMIT = 15 * 1024 * 1024


class Problem(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_bytes(url, data=None):
    # Never forward credentials through redirects or to another host.
    req = Request(url, data=data, headers={'User-Agent': 'PrivateArcheBridge/1.0'})
    with build_opener(NoRedirect).open(req, timeout=18) as response:
        body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise Problem(413, 'Document trop volumineux (limite 15 Mio).')
        return body


def moodle(function, **params):
    fields = dict(wstoken=os.environ['MOODLE_TOKEN'],
                  moodlewsrestformat='json', wsfunction=function, **params)
    data = json.loads(request_bytes(BASE + '/webservice/rest/server.php',
                                   urlencode(fields).encode()))
    if isinstance(data, dict) and ('exception' in data or 'errorcode' in data):
        # Do not return upstream messages or URLs that could contain tokens.
        code = str(data.get('errorcode', 'erreur_api'))
        raise Problem(502, 'ARCHE refuse la requête : ' + code)
    return data


def plain(value):
    value = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', value or '', flags=re.S | re.I)
    value = re.sub(r'<[^>]+>', ' ', value)
    return re.sub(r'\s+', ' ', html.unescape(value)).strip()


def clean_url(value):
    u = urlsplit(value)
    query = parse_qs(u.query)
    safe = {'id': query['id']} if 'id' in query else {}
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(safe, doseq=True), ''))


def courses():
    info = moodle('core_webservice_get_site_info')
    return moodle('core_enrol_get_users_courses', userid=info['userid'])


def contents(course_id):
    if course_id not in {int(c['id']) for c in courses()}:
        raise Problem(403, 'Cours absent de tes inscriptions.')
    return moodle('core_course_get_contents', courseid=course_id)


def pdf_text(course_id, module_id, file_index, start, count, offset):
    modules = [m for s in contents(course_id) for m in s.get('modules', [])]
    module = next((m for m in modules if int(m['id']) == module_id), None)
    if module is None:
        raise Problem(404, 'Ressource introuvable dans ce cours.')
    files = module.get('contents', [])
    if file_index < 0 or file_index >= len(files):
        raise Problem(404, 'Index de fichier incorrect.')
    item = files[file_index]
    if not item.get('filename', '').lower().endswith('.pdf'):
        raise Problem(415, 'Cette version lit uniquement les fichiers PDF texte.')
    u = urlsplit(item.get('fileurl', ''))
    if u.scheme != 'https' or u.netloc != 'arche.univ-lorraine.fr':
        raise Problem(403, 'Les documents externes ne sont pas pris en charge.')
    path = u.path
    if path.startswith('/pluginfile.php/'):
        path = '/webservice' + path
    if not path.startswith('/webservice/pluginfile.php/'):
        raise Problem(415, 'Adresse de téléchargement Moodle non prise en charge.')
    query = parse_qs(u.query)
    query['token'] = [os.environ['MOODLE_TOKEN']]
    url = urlunsplit(('https', u.netloc, path, urlencode(query, doseq=True), ''))
    raw = request_bytes(url)
    if not raw.startswith(b'%PDF-'):
        raise Problem(502, 'ARCHE ne renvoie pas un PDF. Vérifie les droits de téléchargement du jeton.')
    reader = PdfReader(io.BytesIO(raw))
    if start < 1 or start > len(reader.pages):
        raise Problem(400, 'Numéro de page incorrect.')
    pages = []
    for number in range(start - 1, min(start - 1 + count, len(reader.pages))):
        text = reader.pages[number].extract_text() or ''
        pages.append({'page': number + 1, 'text': text[offset:offset + 10000],
                      'next_offset': offset + 10000 if len(text) > offset + 10000 else None})
    return {'filename': item['filename'], 'total_pages': len(reader.pages),
            'source': clean_url(module.get('url', BASE)), 'pages': pages,
            'note': 'Texte extrait automatiquement. Les figures et scans nécessitent une lecture visuelle.'}


def schema():
    def integer(name, default=None):
        spec = {'type': 'integer', 'minimum': 0 if name in ('file_index', 'offset') else 1}
        if default is not None:
            spec['default'] = default
        return {'name': name, 'in': 'query', 'required': default is None, 'schema': spec}
    def operation(name, description, params):
        return {'get': {'operationId': name, 'description': description,
                        'parameters': params, 'x-openai-isConsequential': False,
                        'responses': {'200': {'description': 'Résultat JSON', 'content': {
                            'application/json': {'schema': {'type': 'object', 'additionalProperties': True}}}}}}}
    return {'openapi': '3.1.0', 'info': {'title': 'Mes cours ARCHE', 'version': '1.0.0'},
            'servers': [{'url': os.environ['PUBLIC_URL'].rstrip('/')}],
            'security': [{'BridgeKey': []}], 'components': {'securitySchemes': {
                'BridgeKey': {'type': 'http', 'scheme': 'bearer'}}},
            'paths': {
                '/courses': operation('listCourses', 'Liste mes cours inscrits.', []),
                '/contents': operation('listContents', 'Liste les sections, consignes et fichiers du cours. Conserve les module_id et file_index.', [integer('course_id')]),
                '/pdf': operation('readPdf', 'Lit 1 à 3 pages du PDF identifié par les index de listContents. Répéter pour parcourir le document. offset permet de continuer une page tronquée.', [integer('course_id'), integer('module_id'), integer('file_index', 0), integer('start', 1), integer('count', 2), integer('offset', 0)])}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No request or token logging.

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path == '/':
                return self.send_json(200, {'status': 'Serveur ARCHE en ligne'})
            if path == '/openapi.json':
                return self.send_json(200, schema())
            given = self.headers.get('Authorization', '')
            if not hmac.compare_digest(given.encode(), ('Bearer ' + os.environ['BRIDGE_KEY']).encode()):
                raise Problem(401, 'Clé du serveur absente ou incorrecte.')
            expiry = datetime.fromisoformat(os.environ['ACCESS_UNTIL'].replace('Z', '+00:00'))
            if datetime.now(timezone.utc) >= expiry:
                raise Problem(403, 'Accès arrivé à expiration.')
            q = parse_qs(urlsplit(self.path).query)
            def num(name, default=None):
                try:
                    value = int(q.get(name, [default])[0])
                    if value < 0:
                        raise ValueError()
                    return value
                except (TypeError, ValueError):
                    raise Problem(400, 'Paramètre entier incorrect : ' + name)
            if path == '/courses':
                result = {'courses': [{'id': c['id'], 'name': c['fullname']} for c in courses()]}
            elif path == '/contents':
                result = {'sections': [{'name': s.get('name'), 'summary': plain(s.get('summary')),
                    'modules': [{'module_id': m['id'], 'type': m.get('modname'), 'name': m['name'],
                        'description': plain(m.get('description')), 'source': clean_url(m.get('url', BASE)),
                        'files': [{'file_index': i, 'name': f.get('filename'), 'type': f.get('type')}
                                  for i, f in enumerate(m.get('contents', []))]}
                        for m in s.get('modules', [])]} for s in contents(num('course_id'))]}
            elif path == '/pdf':
                count = num('count', 2)
                if count < 1 or count > 3:
                    raise Problem(400, 'count doit être compris entre 1 et 3.')
                result = pdf_text(num('course_id'), num('module_id'), num('file_index', 0),
                                  num('start', 1), count, num('offset', 0))
            else:
                raise Problem(404, 'Route inconnue.')
            self.send_json(200, result)
        except Problem as exc:
            self.send_json(exc.status, {'error': exc.message})
        except Exception:
            self.send_json(502, {'error': 'Lecture impossible. Vérifie configuration, droits API, réseau ou format du PDF.'})

    def send_json(self, status, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == '__main__':
    for key in ('MOODLE_TOKEN', 'BRIDGE_KEY', 'PUBLIC_URL', 'ACCESS_UNTIL'):
        if not os.environ.get(key):
            raise SystemExit('Variable manquante : ' + key)
    if len(os.environ['BRIDGE_KEY']) < 32:
        raise SystemExit('BRIDGE_KEY doit contenir au moins 32 caractères aléatoires.')
    expiry = datetime.fromisoformat(os.environ['ACCESS_UNTIL'].replace('Z', '+00:00'))
    if expiry.tzinfo is None:
        raise SystemExit('ACCESS_UNTIL doit inclure un fuseau horaire.')
    ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '10000'))), Handler).serve_forever()
