"""MiniRM domain services. All mutations run inside the caller's transaction."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


FIELDS = ('title', 'body', 'kind', 'priority', 'source_ref', 'rationale', 'acceptance', 'parent_req_id')
ROLES = {'admin', 'analyst', 'reviewer', 'developer'}


class DomainError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
        super().__init__(message)


def ensure(condition, message, status=400):
    if not condition:
        raise DomainError(message, status)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def integer(value, label='标识'):
    ensure(isinstance(value, int) and not isinstance(value, bool) and value > 0, f'{label}必须为正整数')
    return value


def text_field(value, label, maximum=20000, required=False):
    ensure(isinstance(value, str), f'{label}必须为文本')
    value = value.strip()
    ensure(len(value) <= maximum, f'{label}超过长度限制 {maximum}')
    ensure(not required or bool(value), f'请填写{label}')
    return value


def password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 250000).hex()
    return f'pbkdf2_sha256$250000${salt}${digest}'


def password_matches(password, stored):
    _, rounds, salt, expected = stored.split('$')
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return hmac.compare_digest(actual, expected)


class Store:
    def __init__(self, path, seed=True):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db:
            db.executescript((Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8'))
        if seed:
            with self.transaction(True) as db:
                if not db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                    self.seed(db)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=10000')
        return db

    @contextmanager
    def transaction(self, write=False):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def seed(self, db):
        password = os.environ.get('MINIRM_DEMO_PASSWORD', 'MiniRM2026!')
        names = [('admin', '空间管理员'), ('analyst', '需求分析员'), ('reviewer', '独立评审者'), ('developer', '开发测试成员')]
        for login, name in names:
            db.execute('INSERT INTO users(login,name,password_hash) VALUES(?,?,?)', (login, name, password_hash(password)))
        db.execute('INSERT INTO workspaces(id,name,description,created_at) VALUES(1,?,?,?)',
                   ('MiniRM 课程演示', '依据 SSAD 作业 2.1 第二版，实现从需求到验证证据的受控管理。', stamp()))
        db.execute('INSERT INTO workspaces(id,name,description,created_at) VALUES(2,?,?,?)',
                   ('权限隔离空间', '仅管理员可读，用于演示跨空间访问控制。', stamp()))
        for uid, (role, _) in enumerate(names, 1):
            db.execute('INSERT INTO memberships VALUES(?,?,?,1)', (1, uid, role))
        db.execute("INSERT INTO memberships VALUES(2,1,'admin',1)")
        analyst, reviewer, dev = Service(db, 2, 1), Service(db, 3, 1), Service(db, 4, 1)
        examples = [
            dict(title='导出指定基线的需求', body='具有空间读取权限的成员选择已建立基线后，系统应导出该基线的需求编号、标题、正文、版本、来源和验收条件。',
                 acceptance='导出 B1，与冻结版本逐项核对；修改当前需求后再次导出 B1，内容保持一致。', kind='functional', priority='Should'),
            dict(title='拒绝跨空间读取与导出', body='没有目标空间成员资格的用户请求读取需求或导出基线时，系统应返回 403，且不返回该空间的业务内容。',
                 acceptance='使用演示分析员访问隔离空间的需求和基线接口，均返回 403。', kind='constraint', priority='Must'),
        ]
        version_ids = []
        for i, content in enumerate(examples, 1):
            content.update(source_ref='2.1 第二版 / 第 9 节 / EX-01', rationale='保证需求及其确认依据可追踪。')
            req = analyst.create_requirement(content)
            vid = req['current_version_id']
            review = analyst.submit_review(vid, 3)
            reviewer.decide_review(review['id'], 'approved', '演示数据：该需求已完成示例评审。')
            for typ, key, title in [('design', f'DES-0{i}', '系统设计修订'), ('verification', f'TC-0{i}', '示例验证证据')]:
                art = dev.create_artifact(dict(type=typ, external_key=key, title=title, version_ref='rev1', uri='',
                    verification_method='inspection' if typ == 'verification' else '', result='passed' if typ == 'verification' else 'not_applicable'))
                link = dev.create_link(dict(source_version_id=vid, artifact_id=art['id'], type='verified_by' if typ == 'verification' else 'implemented_by'))
                reviewer.check_link(link['id'])
            version_ids.append(vid)
        reviewer.create_baseline('B1 初始确认', version_ids)
        analyst.create_requirement(dict(title='多智能体辅助需求审查', body='系统应快速提供友好的智能需求审查，并自动改进需求。',
            kind='functional', priority='Could', source_ref='2.2 作业说明', rationale='辅助发现含糊表达和验证缺口。', acceptance=''))
        # Seeded artifacts are examples, not evidence that automated tests ran.


class Service:
    def __init__(self, db, user_id, workspace_id):
        self.db, self.uid, self.ws = db, integer(user_id), integer(workspace_id, '空间标识')
        member = db.execute('SELECT m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE workspace_id=? AND user_id=? AND m.active=1 AND u.active=1', (self.ws, self.uid)).fetchone()
        ensure(member is not None, '没有此工作空间的访问权限', 403)
        self.role = member['role']

    def permit(self, *roles):
        ensure(self.role in roles, '当前角色没有执行此操作的权限', 403)

    def row(self, sql, args=()):
        result = self.db.execute(sql, args).fetchone()
        ensure(result is not None, '记录不存在或不属于此空间', 404)
        return dict(result)

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def audit(self, action, typ, obj, before=None, after=None, details=None):
        self.db.execute('INSERT INTO audit_events(workspace_id,actor_id,action,object_type,object_id,before_ref,after_ref,details,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)',
                        (self.ws, self.uid, action, typ, obj, str(before) if before is not None else None,
                         str(after) if after is not None else None, encode(details or {}), stamp()))

    def requirement(self, rid):
        return self.row('SELECT r.*,v.title,v.body,v.kind,v.priority,v.review_state,v.version_no,v.source_ref,v.rationale,v.acceptance,v.parent_req_id,v.created_by,v.created_at FROM requirements r JOIN requirement_versions v ON v.id=r.current_version_id WHERE r.id=? AND r.workspace_id=?', (integer(rid), self.ws))

    def version(self, vid):
        return self.row('SELECT v.*,r.workspace_id,r.code,r.archived,r.current_version_id FROM requirement_versions v JOIN requirements r ON r.id=v.req_id WHERE v.id=? AND r.workspace_id=?', (integer(vid), self.ws))

    def content(self, data, complete=False):
        ensure(isinstance(data, dict), '需求内容必须为对象')
        ensure(not (set(data) - set(FIELDS)), '需求内容包含不允许直接修改的字段')
        result = {key: text_field(data.get(key, ''), key, 200 if key == 'title' else 20000, key in ('title', 'body')) for key in FIELDS if key != 'parent_req_id'}
        ensure(result['kind'] in ('functional', 'quality', 'constraint'), '请选择有效需求类型')
        ensure(result['priority'] in ('Must', 'Should', 'Could'), '请选择有效优先级')
        parent = data.get('parent_req_id')
        result['parent_req_id'] = integer(parent, '父需求') if parent is not None else None
        if complete:
            ensure(all(result[k] for k in ('source_ref', 'rationale', 'acceptance')), '提交前必须补齐来源、理由和验收条件')
            ensure(not any(marker in result[key] for marker in ('待确认', '待分析员确认') for key in ('body', 'acceptance', 'source_ref')), '请先解决候选内容中的待确认项，再提交评审')
        return result

    def validate_parents(self, overrides):
        mapping = {r['id']: r['parent_req_id'] for r in self.rows('SELECT r.id,v.parent_req_id FROM requirements r JOIN requirement_versions v ON v.id=r.current_version_id WHERE r.workspace_id=?', (self.ws,))}
        mapping.update(overrides)
        for rid, parent in overrides.items():
            ensure(parent is None or parent in mapping, '父需求必须属于当前空间')
            current, seen = rid, set()
            while current is not None:
                ensure(current not in seen, '父子层次不能形成循环')
                seen.add(current)
                current = mapping.get(current)

    def insert_version(self, rid, data, state='draft'):
        number = self.db.execute('SELECT COALESCE(MAX(version_no),0)+1 FROM requirement_versions WHERE req_id=?', (rid,)).fetchone()[0]
        columns = ','.join(FIELDS)
        return self.db.execute(f'INSERT INTO requirement_versions(req_id,version_no,{columns},review_state,created_by,created_at) VALUES({",".join("?" for _ in range(13))})',
            (rid, number, *(data[k] for k in FIELDS), state, self.uid, stamp())).lastrowid

    def create_requirement(self, data):
        self.permit('analyst')
        content = self.content(data)
        next_number = self.db.execute('SELECT COUNT(*)+1 FROM requirements WHERE workspace_id=?', (self.ws,)).fetchone()[0]
        code = f'RQ-{next_number:03d}'
        rid = self.db.execute('INSERT INTO requirements(workspace_id,code,owner_id) VALUES(?,?,?)', (self.ws, code, self.uid)).lastrowid
        self.validate_parents({rid: content['parent_req_id']})
        vid = self.insert_version(rid, content)
        self.db.execute('UPDATE requirements SET current_version_id=? WHERE id=?', (vid, rid))
        self.audit('requirement.created', 'requirement', rid, after=vid)
        return self.requirement(rid)

    def revise(self, rid, expected, data):
        self.permit('analyst')
        req = self.requirement(rid)
        ensure(not req['archived'], '归档需求不可编辑', 409)
        ensure(req['current_version_id'] == integer(expected), '当前版本已变化，请刷新后重新比较', 409)
        ensure(req['review_state'] in ('draft', 'returned'), '待评审版本须先撤回；已批准版本须走变更申请', 409)
        content = self.content(data)
        self.validate_parents({rid: content['parent_req_id']})
        vid = self.insert_version(rid, content)
        self.db.execute('UPDATE requirements SET current_version_id=? WHERE id=?', (vid, rid))
        self.mark_impacted([expected])
        self.audit('version.created', 'requirement', rid, before=expected, after=vid)
        return self.requirement(rid)

    def reviewer(self, reviewer_id, forbidden):
        integer(reviewer_id, '评审者')
        member = self.db.execute("SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id WHERE workspace_id=? AND user_id=? AND role='reviewer' AND m.active=1 AND u.active=1", (self.ws, reviewer_id)).fetchone()
        ensure(member is not None, '指定用户必须是此空间的活动评审者')
        ensure(reviewer_id not in forbidden, '作者或提交者不得批准自己的需求或变更', 403)

    def submit_review(self, vid, reviewer_id):
        self.permit('analyst')
        version = self.version(vid)
        ensure(not version['archived'] and version['current_version_id'] == vid, '只能提交当前活动版本', 409)
        ensure(version['review_state'] == 'draft', '只能提交草稿版本', 409)
        self.content({k: version[k] for k in FIELDS}, complete=True)
        self.reviewer(reviewer_id, {self.uid, version['created_by']})
        review_id = self.db.execute("INSERT INTO reviews(version_id,submitted_by_id,reviewer_id,decision,submitted_at) VALUES(?,?,?,'pending',?)", (vid, self.uid, reviewer_id, stamp())).lastrowid
        self.db.execute("UPDATE requirement_versions SET review_state='pending' WHERE id=?", (vid,))
        self.audit('review.submitted', 'review', review_id, after=vid)
        return self.row('SELECT * FROM reviews WHERE id=?', (review_id,))

    def get_review(self, review_id):
        return self.row('SELECT rv.* FROM reviews rv JOIN requirement_versions v ON v.id=rv.version_id JOIN requirements r ON r.id=v.req_id WHERE rv.id=? AND r.workspace_id=?', (integer(review_id), self.ws))

    def decide_review(self, review_id, decision, comment):
        self.permit('reviewer')
        review = self.get_review(review_id)
        version = self.version(review['version_id'])
        ensure(review['reviewer_id'] == self.uid, '只有指定评审者可以作出结论', 403)
        ensure(self.uid not in {review['submitted_by_id'], version['created_by']}, '不得自批', 403)
        ensure(review['decision'] == 'pending' and version['review_state'] == 'pending', '评审已经结束', 409)
        ensure(decision in ('approved', 'returned'), '评审结论无效')
        comment = text_field(comment, '评审意见', required=True)
        self.db.execute('UPDATE reviews SET decision=?,comment=?,reviewed_at=? WHERE id=?', (decision, comment, stamp(), review_id))
        self.db.execute('UPDATE requirement_versions SET review_state=? WHERE id=?', (decision, version['id']))
        self.audit(f'review.{decision}', 'review', review_id, after=version['id'], details={'comment': comment})
        return self.get_review(review_id)

    def withdraw_review(self, review_id, reason):
        self.permit('analyst')
        review = self.get_review(review_id)
        ensure(review['submitted_by_id'] == self.uid, '只有原提交者可以撤回', 403)
        ensure(review['decision'] == 'pending', '评审已经结束', 409)
        reason = text_field(reason, '撤回理由', required=True)
        self.db.execute("UPDATE reviews SET decision='withdrawn',comment=?,reviewed_at=? WHERE id=?", (reason, stamp(), review_id))
        self.db.execute("UPDATE requirement_versions SET review_state='draft' WHERE id=?", (review['version_id'],))
        self.audit('review.withdrawn', 'review', review_id, details={'reason': reason})
        return self.get_review(review_id)

    def archive(self, rid, expected, reason):
        self.permit('analyst')
        req = self.requirement(rid)
        ensure(req['current_version_id'] == integer(expected), '当前版本已变化', 409)
        ensure(req['review_state'] != 'pending', '待评审需求须先撤回', 409)
        reason = text_field(reason, '归档原因', required=True)
        self.db.execute('UPDATE requirements SET archived=1 WHERE id=?', (rid,))
        self.audit('requirement.archived', 'requirement', rid, details={'reason': reason})
        return self.requirement(rid)

    def create_artifact(self, data):
        self.permit('analyst', 'developer')
        typ = data.get('type')
        ensure(typ in ('design', 'verification'), '工件类型无效')
        result, method = data.get('result'), data.get('verification_method', '')
        ensure(result in ('not_run', 'passed', 'failed', 'not_applicable'), '工件结果无效')
        if typ == 'verification':
            ensure(method in ('test', 'inspection', 'analysis') and result != 'not_applicable', '验证工件须指定验证方法及结果')
        else:
            ensure(result == 'not_applicable', '设计工件的验证结果须为不适用')
        uri = text_field(data.get('uri', ''), '工件地址', 2000)
        ensure(not uri or uri.startswith(('https://', 'http://')), '工件地址仅允许 http 或 https')
        aid = self.db.execute('INSERT INTO artifacts(workspace_id,type,external_key,title,version_ref,uri,verification_method,result,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (self.ws, typ, text_field(data.get('external_key'), '工件编号', 100, True), text_field(data.get('title'), '工件标题', 200, True),
             text_field(data.get('version_ref'), '工件修订', 200, True), uri, method, result, self.uid, stamp())).lastrowid
        self.audit('artifact.created', 'artifact', aid)
        return self.row('SELECT * FROM artifacts WHERE id=?', (aid,))

    def create_link(self, data):
        self.permit('analyst', 'developer')
        source = self.version(data.get('source_version_id'))
        ensure(not source['archived'], '归档需求不可增加工作关系', 409)
        tid, aid, typ = data.get('target_version_id'), data.get('artifact_id'), data.get('type')
        ensure((tid is not None) != (aid is not None), '目标版本和工件必须二选一')
        sid = source['id']
        if tid is not None:
            target = self.version(tid)
            ensure(typ in ('derives_from', 'depends_on', 'conflicts_with'), '此类型不能连接需求版本')
            ensure(source['req_id'] != target['req_id'], '不得连接同一需求的不同版本')
            if typ == 'conflicts_with':
                sid, tid = sorted((sid, tid))
            else:
                graph = {}
                for link in self.links():
                    if link['active'] and link['type'] in ('derives_from', 'depends_on'):
                        graph.setdefault(link['source_version_id'], []).append(link['target_version_id'])
                pending, seen = [tid], set()
                while pending:
                    node = pending.pop()
                    ensure(node != sid, '依赖或派生关系不能形成循环')
                    if node not in seen:
                        seen.add(node)
                        pending.extend(graph.get(node, []))
        else:
            artifact = self.row('SELECT * FROM artifacts WHERE id=? AND workspace_id=?', (integer(aid), self.ws))
            ensure((typ == 'implemented_by' and artifact['type'] == 'design') or (typ == 'verified_by' and artifact['type'] == 'verification'), '关系类型与工件类型不匹配')
        lid = self.db.execute('INSERT INTO trace_links(source_version_id,target_version_id,artifact_id,type,created_by) VALUES(?,?,?,?,?)', (sid, tid, aid, typ, self.uid)).lastrowid
        self.audit('link.created', 'trace_link', lid)
        return self.row('SELECT * FROM trace_links WHERE id=?', (lid,))

    def links(self):
        return self.rows('SELECT l.*,r.code source_code,v.version_no source_version_no,rt.code target_code,vt.version_no target_version_no,a.external_key,a.title artifact_title,a.version_ref,a.type artifact_type,a.result,a.verification_method FROM trace_links l JOIN requirement_versions v ON v.id=l.source_version_id JOIN requirements r ON r.id=v.req_id LEFT JOIN requirement_versions vt ON vt.id=l.target_version_id LEFT JOIN requirements rt ON rt.id=vt.req_id LEFT JOIN artifacts a ON a.id=l.artifact_id WHERE r.workspace_id=? ORDER BY l.id', (self.ws,))

    def check_link(self, lid):
        self.permit('reviewer')
        link = self.row('SELECT l.* FROM trace_links l JOIN requirement_versions v ON v.id=l.source_version_id JOIN requirements r ON r.id=v.req_id WHERE l.id=? AND r.workspace_id=?', (integer(lid), self.ws))
        ensure(link['active'], '停用的链接不可复核', 409)
        self.db.execute('UPDATE trace_links SET suspect=0,checked_by=?,checked_at=? WHERE id=?', (self.uid, stamp(), lid))
        self.audit('link.checked', 'trace_link', lid)
        return {'id': lid, 'suspect': 0}

    def deactivate_link(self, lid, reason):
        self.permit('analyst', 'developer', 'reviewer')
        self.row('SELECT l.id FROM trace_links l JOIN requirement_versions v ON v.id=l.source_version_id JOIN requirements r ON r.id=v.req_id WHERE l.id=? AND r.workspace_id=?', (integer(lid), self.ws))
        reason = text_field(reason, '停用原因', required=True)
        self.db.execute('UPDATE trace_links SET active=0 WHERE id=?', (lid,))
        self.audit('link.deactivated', 'trace_link', lid, details={'reason': reason})
        return {'id': lid, 'active': 0}

    def impact(self, vids):
        for vid in vids:
            self.version(vid)
        links = [l for l in self.links() if l['active']]
        current = {r['current_version_id'] for r in self.rows('SELECT current_version_id FROM requirements WHERE workspace_id=? AND archived=0', (self.ws,))}
        reached, frontier, affected_links = set(vids), list(vids), set()
        while frontier:
            node = frontier.pop()
            for link in links:
                if link['source_version_id'] == node:
                    affected_links.add(link['id'])
                if link['target_version_id'] == node and link['source_version_id'] in current:
                    affected_links.add(link['id'])
                    if link['type'] in ('derives_from', 'depends_on', 'conflicts_with') and link['source_version_id'] not in reached:
                        reached.add(link['source_version_id'])
                        frontier.append(link['source_version_id'])
                if link['type'] == 'conflicts_with' and link['source_version_id'] == node and link['target_version_id'] in current:
                    if link['target_version_id'] not in reached:
                        reached.add(link['target_version_id'])
                        frontier.append(link['target_version_id'])
        return {'versions': [self.version(vid) for vid in sorted(reached)],
                'links': [l for l in links if l['id'] in affected_links],
                'note': '入向依赖与间接依赖需要复核；原端点不会自动重定向。'}

    def mark_impacted(self, vids):
        impact = self.impact(vids)
        for link in impact['links']:
            self.db.execute('UPDATE trace_links SET suspect=1,checked_by=NULL,checked_at=NULL WHERE id=?', (link['id'],))
        return impact

    def baseline(self, bid):
        base = self.row('SELECT * FROM baselines WHERE id=? AND workspace_id=?', (integer(bid), self.ws))
        base['snapshot'] = json.loads(base.pop('relation_snapshot'))
        base['items'] = self.rows('SELECT v.*,r.code FROM baseline_items bi JOIN requirement_versions v ON v.id=bi.version_id JOIN requirements r ON r.id=bi.req_id WHERE bi.baseline_id=? ORDER BY r.code', (bid,))
        # Review state at freeze time is stored with the snapshot, not read from mutable work relations.
        base['metrics'] = self.metrics(base['items'], base['snapshot']['links'])
        return base

    def create_baseline(self, name, vids):
        self.permit('reviewer')
        name = text_field(name, '基线名称', 200, True)
        ensure(isinstance(vids, list) and 0 < len(vids) <= 2000, '请选择 1 至 2000 个具体版本')
        versions = [self.version(vid) for vid in vids]
        ensure(len({v['req_id'] for v in versions}) == len(versions), '每条需求在基线中只能有一个版本')
        ensure(all(v['review_state'] == 'approved' for v in versions), '基线只能包含已批准版本', 409)
        chosen = {v['req_id']: v['id'] for v in versions}
        selected = set(vids)
        parents = {v['req_id']: v['parent_req_id'] for v in versions}
        for rid in parents:
            node, seen = rid, set()
            while node is not None:
                ensure(node in parents, '基线必须包含所选版本的父需求', 409)
                ensure(node not in seen, '基线父子结构存在循环', 409)
                seen.add(node)
                node = parents[node]
        links = [l for l in self.links() if l['active'] and (l['source_version_id'] in selected or (l['type'] == 'conflicts_with' and l['target_version_id'] in selected))]
        graph = {}
        for link in links:
            ensure(not link['suspect'], '基线存在尚未复核的链接', 409)
            if link['type'] in ('derives_from', 'depends_on'):
                target = self.version(link['target_version_id'])
                ensure(chosen.get(target['req_id']) == target['id'], '依赖必须指向基线中选定的确切版本', 409)
                graph.setdefault(link['source_version_id'], []).append(target['id'])
            if link['type'] == 'conflicts_with':
                ensure(not (link['source_version_id'] in selected and link['target_version_id'] in selected), '所选版本之间仍有未解决的冲突', 409)
        def visit(node, path, done):
            ensure(node not in path, '基线依赖图存在循环', 409)
            if node not in done:
                for target in graph.get(node, []):
                    visit(target, path | {node}, done)
                done.add(node)
        done = set()
        for vid in selected:
            visit(vid, set(), done)
        snapshot = {'schema_version': 1, 'versions': [{'req_id': v['req_id'], 'version_id': v['id'], 'parent_req_id': v['parent_req_id'], 'review_state': 'approved'} for v in versions], 'links': links,
                    'artifacts': [self.row('SELECT * FROM artifacts WHERE id=? AND workspace_id=?', (aid, self.ws)) for aid in sorted({l['artifact_id'] for l in links if l['artifact_id']})]}
        bid = self.db.execute('INSERT INTO baselines(workspace_id,name,created_by,created_at,relation_snapshot) VALUES(?,?,?,?,?)', (self.ws, name, self.uid, stamp(), encode(snapshot))).lastrowid
        self.db.executemany('INSERT INTO baseline_items VALUES(?,?,?)', [(bid, v['req_id'], v['id']) for v in versions])
        self.audit('baseline.created', 'baseline', bid, details={'versions': vids})
        return self.baseline(bid)

    def revoke_baseline(self, bid, reason):
        self.permit('reviewer')
        base = self.baseline(bid)
        ensure(not base['revoked'], '基线已撤销', 409)
        reason = text_field(reason, '撤销原因', required=True)
        self.db.execute('UPDATE baselines SET revoked=1,revoke_reason=? WHERE id=?', (reason, bid))
        self.audit('baseline.revoked', 'baseline', bid, details={'reason': reason})
        return self.baseline(bid)

    def compare_baselines(self, left, right):
        a, b = self.baseline(left), self.baseline(right)
        before = {v['req_id']: v for v in a['items']}
        after = {v['req_id']: v for v in b['items']}
        changes = []
        for rid in sorted(set(before) | set(after)):
            old, new = before.get(rid), after.get(rid)
            if not old or not new or old['id'] != new['id']:
                changes.append({'req_id': rid, 'before': old, 'after': new,
                    'fields': {k: {'before': old.get(k) if old else None, 'after': new.get(k) if new else None} for k in FIELDS if not old or not new or old[k] != new[k]}})
        return {'left': a['name'], 'right': b['name'], 'changes': changes,
                'relations_changed': a['snapshot']['links'] != b['snapshot']['links']}

    def change(self, cid):
        result = self.row('SELECT * FROM change_requests WHERE id=? AND workspace_id=?', (integer(cid), self.ws))
        result['items'] = self.rows('SELECT ci.*,r.code FROM change_items ci JOIN requirements r ON r.id=ci.req_id WHERE ci.change_id=? ORDER BY ci.req_id', (cid,))
        for item in result['items']:
            item['proposed_content'] = json.loads(item['proposed_content'])
            item['base'] = self.version(item['base_version_id'])
        return result

    def create_change(self, data):
        self.permit('analyst')
        reason = text_field(data.get('reason'), '变更理由', required=True)
        summary = text_field(data.get('impact_summary', ''), '影响说明')
        items = data.get('items')
        ensure(isinstance(items, list) and 0 < len(items) <= 100, '变更须包含 1 至 100 个条目')
        cid = self.db.execute("INSERT INTO change_requests(workspace_id,reason,impact_summary,requester_id,status,created_at) VALUES(?,?,?,?,'draft',?)", (self.ws, reason, summary, self.uid, stamp())).lastrowid
        overrides, seen = {}, set()
        for item in items:
            ensure(isinstance(item, dict), '变更项必须为对象')
            req = self.requirement(item.get('req_id'))
            base = self.version(item.get('base_version_id'))
            ensure(req['id'] not in seen, '申请内不能重复修改同一需求')
            seen.add(req['id'])
            ensure(not req['archived'] and base['req_id'] == req['id'] and req['current_version_id'] == base['id'] and base['review_state'] == 'approved', '变更原版本必须是当前活动且已批准的版本', 409)
            content = self.content(item.get('content'), complete=True)
            overrides[req['id']] = content['parent_req_id']
            self.db.execute('INSERT INTO change_items(change_id,req_id,base_version_id,proposed_content) VALUES(?,?,?,?)', (cid, req['id'], base['id'], encode(content)))
        self.validate_parents(overrides)
        self.audit('change.created', 'change', cid)
        return self.change(cid)

    def submit_change(self, cid, reviewer_id, impact_summary):
        self.permit('analyst')
        change = self.change(cid)
        ensure(change['requester_id'] == self.uid, '只有申请人可以提交', 403)
        ensure(change['status'] == 'draft', '只有草稿申请可以提交', 409)
        self.reviewer(reviewer_id, {self.uid})
        summary = text_field(impact_summary, '影响分析说明', required=True)
        for item in change['items']:
            ensure(self.requirement(item['req_id'])['current_version_id'] == item['base_version_id'], '原版本已过期，请新建申请', 409)
        # Reviewer selection is immutable once status becomes pending.
        self.db.execute('UPDATE change_requests SET reviewer_id=?,impact_summary=? WHERE id=?', (reviewer_id, summary, cid))
        self.db.execute("UPDATE change_requests SET status='pending',submitted_at=? WHERE id=?", (stamp(), cid))
        self.audit('change.submitted', 'change', cid)
        return self.change(cid)

    def withdraw_change(self, cid, reason):
        self.permit('analyst')
        change = self.change(cid)
        ensure(change['requester_id'] == self.uid, '只有申请人可以撤回', 403)
        ensure(change['status'] == 'pending', '申请不在待审批状态', 409)
        reason = text_field(reason, '撤回原因', required=True)
        self.db.execute("UPDATE change_requests SET status='draft',submitted_at=NULL WHERE id=?", (cid,))
        self.audit('change.withdrawn', 'change', cid, details={'reason': reason})
        return self.change(cid)

    def decide_change(self, cid, approve, comment):
        self.permit('reviewer')
        change = self.change(cid)
        ensure(change['reviewer_id'] == self.uid and change['requester_id'] != self.uid, '仅指定评审者可审批，且不得自批', 403)
        if change['status'] == 'applied' and approve:
            return change  # retry is idempotent
        ensure(change['status'] == 'pending', '申请不在待审批状态', 409)
        comment = text_field(comment, '审批意见', required=True)
        if not approve:
            self.db.execute("UPDATE change_requests SET status='rejected',comment=?,decided_at=? WHERE id=?", (comment, stamp(), cid))
            self.audit('change.rejected', 'change', cid, details={'comment': comment})
            return self.change(cid)
        items = change['items']
        for item in items:
            req = self.requirement(item['req_id'])
            ensure(not req['archived'] and req['current_version_id'] == item['base_version_id'] and req['review_state'] == 'approved', '存在过期或非活动原版本；整个申请未应用', 409)
            self.content(item['proposed_content'], complete=True)
        self.validate_parents({i['req_id']: i['proposed_content']['parent_req_id'] for i in items})
        self.mark_impacted([i['base_version_id'] for i in items])
        mapping = {}
        for item in items:
            # Result content is authored by the requester, not by the deciding reviewer.
            requester = Service(self.db, change['requester_id'], self.ws)
            vid = requester.insert_version(item['req_id'], item['proposed_content'], 'approved')
            mapping[item['base_version_id']] = vid
            self.db.execute('UPDATE requirements SET current_version_id=? WHERE id=?', (vid, item['req_id']))
            self.db.execute('UPDATE change_items SET result_version_id=? WHERE id=?', (vid, item['id']))
            self.db.execute("INSERT INTO reviews(version_id,submitted_by_id,reviewer_id,change_id,decision,comment,submitted_at,reviewed_at) VALUES(?,?,?,?,'approved',?,?,?)", (vid, change['requester_id'], self.uid, cid, comment, change['submitted_at'], stamp()))
        for link in self.links():
            if not link['active']:
                continue
            affected = link['source_version_id'] in mapping or (link['type'] == 'conflicts_with' and link['target_version_id'] in mapping)
            if affected:
                sid = mapping.get(link['source_version_id'], link['source_version_id'])
                tid = mapping.get(link['target_version_id'], link['target_version_id'])
                if link['type'] == 'conflicts_with' and tid is not None:
                    sid, tid = sorted((sid, tid))
                self.db.execute('INSERT OR IGNORE INTO trace_links(source_version_id,target_version_id,artifact_id,type,created_by,suspect) VALUES(?,?,?,?,?,1)', (sid, tid, link['artifact_id'], link['type'], self.uid))
        self.db.execute("UPDATE change_requests SET status='applied',comment=?,decided_at=? WHERE id=?", (comment, stamp(), cid))
        self.audit('change.applied', 'change', cid, details={'versions': mapping, 'comment': comment})
        return self.change(cid)

    def metrics(self, versions, links):
        approved = [v for v in versions if v['review_state'] == 'approved']
        covered = passed = 0
        for version in approved:
            vid = version.get('current_version_id') if 'req_id' not in version else version['id']
            evidence = [l for l in links if l['source_version_id'] == vid and l['type'] == 'verified_by' and l['active'] and not l['suspect']]
            covered += bool(evidence)
            passed += bool(evidence) and all(l['result'] == 'passed' for l in evidence)
        n = len(approved)
        return {'approved': n, 'covered': covered, 'passed': passed, 'coverage': covered / n if n else None, 'pass_rate': passed / n if n else None}

    def list_requirements(self, query):
        filters, args = ['r.workspace_id=?'], [self.ws]
        if query.get('q'):
            # Escape wildcard syntax: the query is a literal substring, not an SQL LIKE pattern.
            search = query['q'].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            filters.append("(r.code LIKE ? ESCAPE '\\' OR v.title LIKE ? ESCAPE '\\' OR v.body LIKE ? ESCAPE '\\')")
            args.extend([f'%{search}%'] * 3)
        for key in ('kind', 'priority', 'review_state'):
            if query.get(key):
                filters.append(f'v.{key}=?')
                args.append(query[key])
        if query.get('archived') != '1':
            filters.append('r.archived=0')
        try:
            page, size = int(query.get('page', 1)), int(query.get('page_size', 20))
        except (TypeError, ValueError):
            raise DomainError('分页参数必须为整数')
        ensure(page > 0 and 1 <= size <= 100, '分页参数超出范围')
        where = ' AND '.join(filters)
        source = f'FROM requirements r JOIN requirement_versions v ON v.id=r.current_version_id WHERE {where}'
        total = self.db.execute(f'SELECT COUNT(*) {source}', args).fetchone()[0]
        items = self.rows(f'SELECT r.*,v.title,v.body,v.kind,v.priority,v.version_no,v.review_state,v.parent_req_id,v.source_ref,v.rationale,v.acceptance {source} ORDER BY r.id LIMIT ? OFFSET ?', (*args, size, (page-1)*size))
        return {'items': items, 'total': total, 'page': page, 'page_size': size}

    def detail(self, rid):
        req = self.requirement(rid)
        req['versions'] = self.rows('SELECT * FROM requirement_versions WHERE req_id=? ORDER BY version_no DESC', (rid,))
        req['reviews'] = self.rows('SELECT rv.*,u.name reviewer_name FROM reviews rv JOIN requirement_versions v ON v.id=rv.version_id JOIN users u ON u.id=rv.reviewer_id WHERE v.req_id=? ORDER BY rv.id DESC', (rid,))
        req['links'] = [l for l in self.links() if l['source_version_id'] in {v['id'] for v in req['versions']} or l['target_version_id'] in {v['id'] for v in req['versions']}]
        return req

    def state(self):
        workspace = self.row('SELECT * FROM workspaces WHERE id=?', (self.ws,))
        current = self.rows('SELECT r.*,v.title,v.kind,v.priority,v.version_no,v.review_state FROM requirements r JOIN requirement_versions v ON v.id=r.current_version_id WHERE r.workspace_id=? ORDER BY r.id', (self.ws,))
        links = self.links()
        return {'workspace': workspace, 'role': self.role, 'requirements': current, 'links': links,
                'metrics': self.metrics([r for r in current if not r['archived']], links),
                'members': self.rows('SELECT u.id,u.login,u.name,m.role,m.active FROM memberships m JOIN users u ON u.id=m.user_id WHERE workspace_id=?', (self.ws,)),
                'reviews': self.rows('SELECT rv.*,r.code,v.title,v.body,v.acceptance,v.version_no FROM reviews rv JOIN requirement_versions v ON v.id=rv.version_id JOIN requirements r ON r.id=v.req_id WHERE r.workspace_id=? ORDER BY rv.id DESC', (self.ws,)),
                'baselines': self.rows('SELECT id,name,created_at,revoked,revoke_reason FROM baselines WHERE workspace_id=? ORDER BY id DESC', (self.ws,)),
                'changes': self.rows('SELECT * FROM change_requests WHERE workspace_id=? ORDER BY id DESC', (self.ws,)),
                'artifacts': self.rows('SELECT * FROM artifacts WHERE workspace_id=? ORDER BY id DESC', (self.ws,)),
                'audit': self.rows('SELECT e.*,u.name actor FROM audit_events e JOIN users u ON u.id=e.actor_id WHERE workspace_id=? ORDER BY e.id DESC LIMIT 150', (self.ws,)),
                'agent_runs': self.rows('SELECT ar.*,r.code FROM agent_runs ar JOIN requirement_versions v ON v.id=ar.version_id JOIN requirements r ON r.id=v.req_id WHERE ar.workspace_id=? ORDER BY ar.id DESC LIMIT 20', (self.ws,))}

    def update_member(self, user_id, role, active):
        self.permit('admin')
        integer(user_id)
        ensure(role in ROLES and isinstance(active, bool), '成员角色或启用状态无效')
        self.row('SELECT id FROM users WHERE id=?', (user_id,))
        # Keep at least one active administrator; authorship remains separately enforced.
        previous = self.db.execute('SELECT role,active FROM memberships WHERE workspace_id=? AND user_id=?', (self.ws, user_id)).fetchone()
        if previous and previous['role'] == 'admin' and previous['active'] and (role != 'admin' or not active):
            count = self.db.execute("SELECT COUNT(*) FROM memberships WHERE workspace_id=? AND role='admin' AND active=1", (self.ws,)).fetchone()[0]
            ensure(count > 1, '不能移除最后一位活动管理员', 409)
        self.db.execute('INSERT INTO memberships(workspace_id,user_id,role,active) VALUES(?,?,?,?) ON CONFLICT(workspace_id,user_id) DO UPDATE SET role=excluded.role,active=excluded.active', (self.ws, user_id, role, int(active)))
        self.audit('membership.updated', 'user', user_id, details={'role': role, 'active': active})
        return {'user_id': user_id, 'role': role, 'active': active}


def login(db, username, password):
    username = text_field(username, '用户名', 100, True)
    password = text_field(password, '密码', 200, True)
    user = db.execute('SELECT * FROM users WHERE login=? AND active=1', (username,)).fetchone()
    # Unknown users also perform a password derivation to reduce timing differences.
    stored = user['password_hash'] if user else 'pbkdf2_sha256$250000$00000000000000000000000000000000$' + '0'*64
    ensure(password_matches(password, stored) and user is not None, '用户名或密码错误', 401)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute('DELETE FROM sessions WHERE expires_at < ?', (time.time(),))
    db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, time.time()+8*3600))
    return token, csrf, {'id': user['id'], 'name': user['name'], 'login': user['login']}


def session(db, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    row = db.execute('SELECT s.*,u.name,u.login FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=? AND expires_at>? AND u.active=1', (digest, time.time())).fetchone()
    ensure(row is not None, '请先登录', 401)
    return dict(row)
