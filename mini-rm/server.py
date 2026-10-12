"""Local HTTP adapter. No third-party dependencies required."""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from minirm.core import DomainError, FIELDS, Service, Store, encode, ensure, login, session, stamp
from minirm.agents import provider_state, run_pipeline

ROOT = Path(__file__).resolve().parent
LOGIN_ATTEMPTS = {}
LOGIN_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = 'MiniRM/1.0'

    def log_message(self, fmt, *args):
        # Never log request bodies, passwords, session cookies or API keys.
        print(f'{self.address_string()} {fmt % args}', flush=True)

    def send(self, status, data, kind='application/json; charset=utf-8', cookie=None, disposition=None):
        raw = encode(data).encode('utf-8') if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if disposition:
            self.send_header('Content-Disposition', disposition)
        self.end_headers()
        self.wfile.write(raw)

    def token(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get('Cookie', ''))
        except Exception:
            return ''
        return cookies['minirm_session'].value if 'minirm_session' in cookies else ''

    def same_origin(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        ensure(self.headers.get('Host') in (expected, f'localhost:{self.server.server_port}'), 'Host 不受信任', 403)
        origin = self.headers.get('Origin')
        if origin:
            ensure(origin in (f'http://{expected}', f'http://localhost:{self.server.server_port}'), '不允许跨站提交', 403)

    def body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise DomainError('请求长度无效')
        ensure(0 < length <= 200000, '请求内容为空或超过 200KB 限制', 413)
        ensure(self.headers.get('Content-Type', '').split(';')[0] == 'application/json', '仅接受 application/json', 415)
        try:
            value = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise DomainError('请求必须为有效 JSON')
        ensure(isinstance(value, dict), 'JSON 须为对象')
        return value

    def do_GET(self):
        self.handle_request(False)

    def do_POST(self):
        self.handle_request(True)

    def handle_request(self, write):
        try:
            self.same_origin()
            parts = urlsplit(self.path)
            path = parts.path.rstrip('/') or '/'
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            if not path.startswith('/api/'):
                ensure(not write, '方法不允许', 405)
                files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}
                ensure(path in files, '页面不存在', 404)
                file = ROOT / 'static' / files[path]
                kind = {'html': 'text/html', 'js': 'application/javascript', 'css': 'text/css'}[file.suffix[1:]]
                return self.send(200, file.read_bytes(), kind + '; charset=utf-8')
            if path == '/api/health' and not write:
                return self.send(200, {'ok': True, 'app': 'MiniRM'})
            data = self.body() if write else {}
            if path == '/api/login' and write:
                ip = self.client_address[0]
                with LOGIN_LOCK:
                    recent = [t for t in LOGIN_ATTEMPTS.get(ip, []) if t > time.time()-60]
                    ensure(len(recent) < 15, '登录尝试过多，请一分钟后重试', 429)
                    LOGIN_ATTEMPTS[ip] = recent + [time.time()]
                with self.server.store.transaction(True) as db:
                    token, csrf, user = login(db, data.get('login'), data.get('password'))
                return self.send(200, {'user': user, 'csrf': csrf}, cookie=f'minirm_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800')
            if path.endswith('/agents/analyze') and write:
                return self.analyze(path, data)
            with self.server.store.transaction(write) as db:
                auth = session(db, self.token())
                if write:
                    ensure(hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), auth['csrf']), 'CSRF 校验失败，请刷新登录状态', 403)
                if path == '/api/session' and not write:
                    spaces = [dict(r) for r in db.execute('SELECT w.*,m.role FROM workspaces w JOIN memberships m ON w.id=m.workspace_id WHERE m.user_id=? AND m.active=1', (auth['user_id'],))]
                    result = {'user': {'id': auth['user_id'], 'name': auth['name'], 'login': auth['login']}, 'csrf': auth['csrf'], 'workspaces': spaces, 'provider': provider_state()}
                elif path == '/api/logout' and write:
                    db.execute('DELETE FROM sessions WHERE token_hash=?', (auth['token_hash'],))
                    result = {'ok': True}
                else:
                    match = re.fullmatch(r'/api/workspaces/(\d+)(/.*)?', path)
                    ensure(match is not None, '接口不存在', 404)
                    service = Service(db, auth['user_id'], int(match[1]))
                    result = self.dispatch(service, match[2] or '/', write, data, query)
            if isinstance(result, tuple):
                body, filename = result
                return self.send(200, body, disposition=f'attachment; filename="{filename}"')
            return self.send(200, result, cookie='minirm_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0' if path == '/api/logout' else None)
        except DomainError as error:
            self.send(error.status, {'error': error.message})
        except sqlite3.IntegrityError:
            self.send(409, {'error': '数据约束冲突：可能存在重复编号、重复关系或不可修改的历史记录'})
        except sqlite3.OperationalError:
            self.send(503, {'error': '数据库暂时繁忙，请重试；事务未部分写入'})
        except Exception as error:
            print(f'Internal error: {type(error).__name__}', flush=True)
            self.send(500, {'error': '服务器内部错误；本次事务已回滚'})

    def dispatch(self, s, route, write, data, query):
        if route == '/state' and not write:
            return s.state()
        if route == '/requirements':
            return s.create_requirement(data.get('content')) if write else s.list_requirements(query)
        match = re.fullmatch(r'/requirements/(\d+)(?:/(versions|archive))?', route)
        if match:
            rid, action = int(match[1]), match[2]
            if not write and action is None:
                return s.detail(rid)
            if write and action == 'versions':
                return s.revise(rid, data.get('expected_version_id'), data.get('content'))
            if write and action == 'archive':
                return s.archive(rid, data.get('expected_version_id'), data.get('reason'))
        match = re.fullmatch(r'/versions/(\d+)/(reviews|impact)', route)
        if match:
            vid = int(match[1])
            if match[2] == 'reviews' and write:
                return s.submit_review(vid, data.get('reviewer_id'))
            if match[2] == 'impact' and not write:
                return s.impact([vid])
        match = re.fullmatch(r'/reviews/(\d+)/(decide|withdraw)', route)
        if match and write:
            return s.decide_review(int(match[1]), data.get('decision'), data.get('comment')) if match[2] == 'decide' else s.withdraw_review(int(match[1]), data.get('reason'))
        if route == '/artifacts' and write:
            return s.create_artifact(data)
        if route == '/links' and write:
            return s.create_link(data)
        match = re.fullmatch(r'/links/(\d+)/(check|deactivate)', route)
        if match and write:
            return s.check_link(int(match[1])) if match[2] == 'check' else s.deactivate_link(int(match[1]), data.get('reason'))
        if route == '/baselines' and write:
            return s.create_baseline(data.get('name'), data.get('version_ids'))
        if route == '/baselines/compare' and not write:
            try:
                return s.compare_baselines(int(query.get('left', 0)), int(query.get('right', 0)))
            except ValueError:
                raise DomainError('基线标识无效')
        match = re.fullmatch(r'/baselines/(\d+)(?:/(export|revoke))?', route)
        if match:
            bid = int(match[1])
            if match[2] == 'revoke' and write:
                return s.revoke_baseline(bid, data.get('reason'))
            if not write:
                base = s.baseline(bid)
                return (encode(base).encode('utf-8'), f'baseline-{bid}.json') if match[2] == 'export' else base
        if route == '/changes' and write:
            return s.create_change(data)
        match = re.fullmatch(r'/changes/(\d+)(?:/(submit|withdraw|decide|impact))?', route)
        if match:
            cid, action = int(match[1]), match[2]
            if write and action == 'submit':
                return s.submit_change(cid, data.get('reviewer_id'), data.get('impact_summary'))
            if write and action == 'withdraw':
                return s.withdraw_change(cid, data.get('reason'))
            if write and action == 'decide':
                ensure(isinstance(data.get('approve'), bool), '审批动作无效')
                return s.decide_change(cid, data['approve'], data.get('comment'))
            if not write:
                change = s.change(cid)
                return s.impact([i['base_version_id'] for i in change['items']]) if action == 'impact' else change
        if route == '/members' and write:
            return s.update_member(data.get('user_id'), data.get('role'), data.get('active'))
        raise DomainError('接口或方法不存在', 404)

    def analyze(self, path, data):
        match = re.fullmatch(r'/api/workspaces/(\d+)/agents/analyze', path)
        ensure(match is not None, '接口不存在', 404)
        ws = int(match[1])
        with self.server.store.transaction() as db:
            auth = session(db, self.token())
            ensure(hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), auth['csrf']), 'CSRF 校验失败', 403)
            s = Service(db, auth['user_id'], ws)
            s.permit('analyst', 'reviewer', 'developer')
            v = s.version(data.get('version_id'))
            context = {'version': {k: v[k] for k in (*FIELDS, 'id', 'req_id', 'version_no', 'review_state', 'code')}, 'impact': s.impact([v['id']])}
        # Slow external calls never hold SQLite's write lock.
        result = run_pipeline(context, data.get('mode', 'rules'))
        with self.server.store.transaction(True) as db:
            auth = session(db, self.token())
            ensure(hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), auth['csrf']), '登录状态已变化', 403)
            s = Service(db, auth['user_id'], ws)
            s.permit('analyst', 'reviewer', 'developer')
            s.content(result['candidate'])
            result['stale'] = s.version(v['id'])['current_version_id'] != v['id']
            run_id = db.execute('INSERT INTO agent_runs(workspace_id,version_id,actor_id,mode,result,created_at) VALUES(?,?,?,?,?,?)', (ws, v['id'], s.uid, result['mode'], encode(result), stamp())).lastrowid
            s.audit('agents.completed', 'agent_run', run_id, details={'mode': result['mode'], 'version_id': v['id']})
            result['run_id'] = run_id
        return self.send(200, result)


def main():
    parser = argparse.ArgumentParser(description='MiniRM 本地需求管理系统')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', default=str(ROOT / 'data' / 'minirm.sqlite3'))
    args = parser.parse_args()
    store = Store(args.db)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.store = store
    print(f'MiniRM ready: http://127.0.0.1:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
