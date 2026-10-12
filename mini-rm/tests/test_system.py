import copy
import hashlib
import http.cookiejar
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from minirm.core import DomainError, FIELDS, Service, Store, login, session
from minirm.agents import run_pipeline, validate_report
from server import Handler


def content(**overrides):
    value = dict(title='可验证需求', body='用户选择指定基线后，系统返回冻结的需求内容。', kind='functional', priority='Must',
                 source_ref='EX-01 / 第 1 条', rationale='保持历史依据一致', acceptance='比较导出内容与冻结版本，全部字段相同。', parent_req_id=None)
    value.update(overrides)
    return value


class DomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.template = Store(Path(cls.fixture.name)/'template.sqlite3')

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'case.sqlite3'
        src, dest = self.template.connect(), sqlite3.connect(self.path)
        src.backup(dest)
        src.close()
        dest.close()
        self.store = Store(self.path, seed=False)

    def tearDown(self):
        self.temp.cleanup()

    def call(self, uid, method, *args):
        with self.store.transaction(True) as db:
            return getattr(Service(db, uid, 1), method)(*args)

    def count(self, table):
        with self.store.transaction() as db:
            return db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]

    def approved(self, **overrides):
        r = self.call(2, 'create_requirement', content(**overrides))
        review = self.call(2, 'submit_review', r['current_version_id'], 3)
        self.call(3, 'decide_review', review['id'], 'approved', '条件与验收明确')
        return self.call(2, 'requirement', r['id'])

    def change(self, reqs, replacements=None):
        replacements = replacements or {}
        items = []
        for r in reqs:
            proposed = {k:r[k] for k in FIELDS}
            proposed.update(replacements.get(r['id'], {'body':r['body']+' 修订内容。'}))
            items.append(dict(req_id=r['id'], base_version_id=r['current_version_id'], content=proposed))
        c = self.call(2, 'create_change', dict(reason='修改业务范围', impact_summary='检查设计和测试证据', items=items))
        self.call(2, 'submit_change', c['id'], 3, '已检查入向依赖与验证证据')
        return c

    def test_AT01_required_fields_and_rollback(self):
        n, audits = self.count('requirements'), self.count('audit_events')
        with self.assertRaises(DomainError):
            self.call(2, 'create_requirement', content(body=''))
        with self.assertRaises(DomainError):
            self.call(2, 'create_requirement', content(parent_req_id=99999))
        self.assertEqual(n, self.count('requirements'))
        self.assertEqual(audits, self.count('audit_events'))

    def test_AT02_literal_search_and_pagination(self):
        self.call(2, 'create_requirement', content(title='100%_字面查询'))
        result = self.call(2, 'list_requirements', {'q':'%_', 'kind':'functional', 'page_size':1})
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['items'][0]['title'], '100%_字面查询')
        second = self.call(2, 'list_requirements', {'q':'%_', 'page':2, 'page_size':1})
        self.assertEqual(second['items'], [])

    def test_AT03_optimistic_concurrency_preserves_versions(self):
        r = self.call(2, 'create_requirement', content())
        revised = self.call(2, 'revise', r['id'], r['current_version_id'], content(body='第一次修改'))
        with self.assertRaises(DomainError) as error:
            self.call(2, 'revise', r['id'], r['current_version_id'], content(body='过期提交'))
        self.assertEqual(error.exception.status, 409)
        detail = self.call(2, 'detail', r['id'])
        self.assertEqual(len(detail['versions']), 2)
        self.assertEqual(detail['code'], r['code'])
        self.assertEqual(detail['current_version_id'], revised['current_version_id'])

    def test_AT04_return_and_resubmit_new_version(self):
        r = self.call(2, 'create_requirement', content())
        review = self.call(2, 'submit_review', r['current_version_id'], 3)
        self.call(3, 'decide_review', review['id'], 'returned', '补充异常输入')
        new = self.call(2, 'revise', r['id'], r['current_version_id'], content(acceptance='正常与异常输入分别检查'))
        self.call(2, 'submit_review', new['current_version_id'], 3)
        detail = self.call(2, 'detail', r['id'])
        self.assertEqual(detail['versions'][1]['review_state'], 'returned')
        self.assertEqual(detail['reviews'][1]['comment'], '补充异常输入')

    def test_pending_lock_and_withdraw(self):
        r = self.call(2, 'create_requirement', content())
        review = self.call(2, 'submit_review', r['current_version_id'], 3)
        with self.assertRaises(DomainError):
            self.call(2, 'revise', r['id'], r['current_version_id'], content())
        self.call(2, 'withdraw_review', review['id'], '修订内容')
        new = self.call(2, 'revise', r['id'], r['current_version_id'], content(body='新内容'))
        self.assertEqual(new['version_no'], 2)
        self.assertEqual(self.call(2, 'get_review', review['id'])['decision'], 'withdrawn')

    def test_AT05_baseline_blocks_drafts_suspect_and_missing_parent(self):
        draft = self.call(2, 'create_requirement', content())
        count = self.count('baselines')
        with self.assertRaises(DomainError):
            self.call(3, 'create_baseline', 'draft invalid', [draft['current_version_id']])
        a = self.approved()
        b = self.approved(parent_req_id=a['id'])
        with self.assertRaises(DomainError):
            self.call(3, 'create_baseline', 'parent invalid', [b['current_version_id']])
        link = self.call(4, 'create_link', dict(source_version_id=b['current_version_id'], target_version_id=a['current_version_id'], type='depends_on'))
        with self.assertRaises(DomainError):
            self.call(3, 'create_baseline', 'suspect invalid', [a['current_version_id'],b['current_version_id']])
        self.assertEqual(self.count('baselines'), count)
        self.call(3, 'check_link', link['id'])
        self.call(3, 'create_baseline', 'closed valid', [a['current_version_id'],b['current_version_id']])

    def test_baseline_requires_exact_dependency_version(self):
        a, b = self.approved(), self.approved()
        link = self.call(4, 'create_link', dict(source_version_id=b['current_version_id'],target_version_id=a['current_version_id'],type='depends_on'))
        self.call(3,'check_link',link['id'])
        c = self.change([a])
        self.call(3,'decide_change',c['id'],True,'通过')
        self.call(3,'check_link',link['id'])
        new = self.call(2,'requirement',a['id'])
        with self.assertRaises(DomainError) as error:
            self.call(3,'create_baseline','exact invalid',[new['current_version_id'],b['current_version_id']])
        self.assertIn('确切版本',error.exception.message)

    def test_AT06_stale_batch_is_atomic(self):
        a, b = self.approved(), self.approved()
        first = self.change([a,b])
        competing = self.change([b])
        self.call(3,'decide_change',competing['id'],True,'先批准')
        count = self.count('requirement_versions')
        with self.assertRaises(DomainError):
            self.call(3,'decide_change',first['id'],True,'批准过期申请')
        self.assertEqual(self.count('requirement_versions'),count)
        self.assertEqual(self.call(2,'requirement',a['id'])['current_version_id'],a['current_version_id'])
        self.assertEqual(self.call(2,'change',first['id'])['status'],'pending')

    def test_mid_transaction_failure_rolls_back_all_writes(self):
        a, b = self.approved(), self.approved()
        c = self.change([a,b],{b['id']:{'title':'FAIL_INJECTION'}})
        versions,audits,links=self.count('requirement_versions'),self.count('audit_events'),self.count('trace_links')
        with self.store.transaction(True) as db:
            db.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON requirement_versions WHEN NEW.title='FAIL_INJECTION' BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.call(3,'decide_change',c['id'],True,'批准')
        self.assertEqual(self.count('requirement_versions'),versions)
        self.assertEqual(self.count('audit_events'),audits)
        self.assertEqual(self.count('trace_links'),links)
        self.assertEqual(self.call(2,'requirement',a['id'])['current_version_id'],a['current_version_id'])
        self.assertTrue(all(i['result_version_id'] is None for i in self.call(2,'change',c['id'])['items']))

    def test_duplicate_approval_is_idempotent(self):
        a = self.approved()
        c = self.change([a])
        first = self.call(3,'decide_change',c['id'],True,'批准')
        counts=(self.count('requirement_versions'),self.count('reviews'),self.count('audit_events'))
        second = self.call(3,'decide_change',c['id'],True,'重试')
        self.assertEqual(first['items'][0]['result_version_id'],second['items'][0]['result_version_id'])
        self.assertEqual(counts,(self.count('requirement_versions'),self.count('reviews'),self.count('audit_events')))
        with self.store.transaction() as db:
            evidence=db.execute('SELECT * FROM reviews WHERE change_id=?',(c['id'],)).fetchone()
            self.assertEqual(evidence['version_id'],first['items'][0]['result_version_id'])

    def test_AT07_incoming_and_indirect_impact(self):
        a,b,c=self.approved(),self.approved(),self.approved()
        ids=[]
        for source,target in [(b,a),(c,b)]:
            l=self.call(4,'create_link',dict(source_version_id=source['current_version_id'],target_version_id=target['current_version_id'],type='depends_on'))
            self.call(3,'check_link',l['id']);ids.append(l['id'])
        impact=self.call(2,'impact',[a['current_version_id']])
        self.assertEqual({v['req_id'] for v in impact['versions']},{a['id'],b['id'],c['id']})
        change=self.change([a]);self.call(3,'decide_change',change['id'],True,'批准')
        links={l['id']:l for l in self.call(2,'links')}
        self.assertTrue(all(links[i]['suspect'] for i in ids))
        self.assertEqual(links[ids[0]]['target_version_id'],a['current_version_id'])

    def test_AT08_roles_cross_space_and_self_approval(self):
        with self.assertRaises(DomainError):
            self.call(1,'create_requirement',content())
        with self.store.transaction() as db:
            with self.assertRaises(DomainError) as error:
                Service(db,2,2)
            self.assertEqual(error.exception.status,403)
        r=self.call(2,'create_requirement',content())
        self.call(1,'update_member',2,'reviewer',True)
        self.call(1,'update_member',4,'analyst',True)
        with self.assertRaises(DomainError) as error:
            self.call(4,'submit_review',r['current_version_id'],2)
        self.assertEqual(error.exception.status,403)

    def test_last_admin_cannot_be_removed(self):
        with self.assertRaises(DomainError):
            self.call(1,'update_member',1,'analyst',True)

    def test_AT09_old_baseline_content_and_metrics_unchanged(self):
        before=self.call(2,'baseline',1)
        a=self.call(2,'requirement',1)
        c=self.change([a]);self.call(3,'decide_change',c['id'],True,'批准')
        after=self.call(2,'baseline',1)
        self.assertEqual(before,after)
        self.assertEqual(after['items'][0]['version_no'],1)
        self.assertEqual(self.call(2,'requirement',1)['version_no'],2)

    def test_AT10_parent_history_and_immutable_artifacts(self):
        parent=self.approved()
        r=self.call(2,'create_requirement',content())
        self.call(2,'revise',r['id'],r['current_version_id'],content(parent_req_id=parent['id']))
        detail=self.call(2,'detail',r['id'])
        self.assertIsNone(detail['versions'][1]['parent_req_id'])
        self.assertEqual(detail['versions'][0]['parent_req_id'],parent['id'])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction(True) as db:
                db.execute("UPDATE artifacts SET result='failed' WHERE id=2")

    def test_database_enforces_version_and_audit_immutability(self):
        for sql in ["UPDATE requirement_versions SET body='overwrite' WHERE id=1",'DELETE FROM audit_events','UPDATE baseline_items SET version_id=2 WHERE baseline_id=1']:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.transaction(True) as db:db.execute(sql)

    def test_trace_cycles_and_type_mismatch(self):
        a,b=self.approved(),self.approved()
        self.call(4,'create_link',dict(source_version_id=a['current_version_id'],target_version_id=b['current_version_id'],type='depends_on'))
        with self.assertRaises(DomainError):self.call(4,'create_link',dict(source_version_id=b['current_version_id'],target_version_id=a['current_version_id'],type='derives_from'))
        with self.assertRaises(DomainError):self.call(4,'create_link',dict(source_version_id=a['current_version_id'],artifact_id=1,type='verified_by'))

    def test_parent_batch_cycle_is_rejected(self):
        a,b=self.approved(),self.approved()
        with self.assertRaises(DomainError):self.change([a,b],{a['id']:{'parent_req_id':b['id']},b['id']:{'parent_req_id':a['id']}})

    def test_coverage_is_distinct_from_pass_rate(self):
        a=self.approved()
        art=self.call(4,'create_artifact',dict(type='verification',external_key='TC-F',title='失败证据',version_ref='run1',verification_method='test',result='failed'))
        link=self.call(4,'create_link',dict(source_version_id=a['current_version_id'],artifact_id=art['id'],type='verified_by'))
        self.call(3,'check_link',link['id'])
        s=self.call(2,'state')
        self.assertEqual(s['metrics']['covered'],3)
        self.assertEqual(s['metrics']['passed'],2)

    def test_rules_agents_do_not_mutate_business_data(self):
        v=self.call(2,'version',3)
        before=self.count('requirement_versions')
        result=run_pipeline({'version':v,'impact':self.call(2,'impact',[3])},'rules')
        self.assertEqual(len(result['events']),4)
        self.assertEqual(result['mode'],'rules')
        self.assertFalse(result['mutated_business_data'])
        self.assertTrue(result['requires_human_confirmation'])
        self.assertEqual(result['candidate']['source_ref'],v['source_ref'])
        self.assertEqual(self.count('requirement_versions'),before)

    def test_invalid_agent_report_is_rejected(self):
        with self.assertRaises(DomainError):validate_report({'summary':'ok','issues':[{'severity':'critical'}]})
        with self.assertRaises(DomainError):validate_report({'summary':'ok','issues':[],'candidate':{'review_state':'approved'}},True)

    def test_conflict_on_changed_target_is_preserved(self):
        a,b=self.approved(),self.approved()
        l=self.call(4,'create_link',dict(source_version_id=a['current_version_id'],target_version_id=b['current_version_id'],type='conflicts_with'))
        self.call(3,'check_link',l['id'])
        c=self.change([b]);self.call(3,'decide_change',c['id'],True,'内容批准，冲突仍待解决')
        new=self.call(2,'requirement',b['id'])
        conflicts=[l for l in self.call(2,'links') if l['type']=='conflicts_with' and l['target_version_id']==new['current_version_id']]
        self.assertEqual(len(conflicts),1)
        self.assertTrue(conflicts[0]['suspect'])
        self.call(3,'check_link',conflicts[0]['id'])
        with self.assertRaises(DomainError):self.call(3,'create_baseline','conflict invalid',[a['current_version_id'],new['current_version_id']])

    def test_sessions_hash_tokens_and_store_no_plaintext_password(self):
        with self.store.transaction(True) as db:
            token,csrf,user=login(db,'analyst','MiniRM2026!')
            self.assertEqual(session(db,token)['csrf'],csrf)
            stored=db.execute('SELECT token_hash FROM sessions').fetchone()[0]
            self.assertEqual(stored,hashlib.sha256(token.encode()).hexdigest())
            self.assertNotIn('MiniRM2026!',db.execute('SELECT password_hash FROM users WHERE id=2').fetchone()[0])

    def test_unconfirmed_agent_placeholders_cannot_be_submitted(self):
        r=self.call(2,'create_requirement',content(acceptance='待分析员确认：实际验收条件'))
        with self.assertRaises(DomainError):self.call(2,'submit_review',r['current_version_id'],3)
        self.assertEqual(self.call(2,'requirement',r['id'])['review_state'],'draft')

    def test_archiving_preserves_baseline_and_requires_reason(self):
        r=self.call(2,'requirement',1)
        old=self.call(2,'baseline',1)
        with self.assertRaises(DomainError):self.call(2,'archive',1,r['current_version_id'],'')
        self.call(2,'archive',1,r['current_version_id'],'停止维护此需求')
        self.assertEqual(self.call(2,'baseline',1),old)
        self.assertTrue(self.call(2,'requirement',1)['archived'])

    def test_submitted_change_items_are_locked_in_database(self):
        r=self.approved();c=self.change([r])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction(True) as db:db.execute('DELETE FROM change_items WHERE change_id=?',(c['id'],))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.server.store=Store(Path(cls.temp.name)/'http.sqlite3')
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base=f'http://127.0.0.1:{cls.server.server_port}'
        cls.cookies=http.cookiejar.CookieJar()
        cls.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cls.cookies))
        response=cls.request('/api/login',dict(login='analyst',password='MiniRM2026!'))
        cls.csrf=response[1]['csrf']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    @classmethod
    def request(cls,path,body=None,headers=None):
        headers={'Content-Type':'application/json',**(headers or {})}
        req=urllib.request.Request(cls.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        try:
            with cls.client.open(req) as r:return r.status,json.loads(r.read())
        except urllib.error.HTTPError as e:return e.code,json.loads(e.read())

    def test_http_missing_csrf_and_cross_origin_rejected(self):
        body={'content':content()}
        self.assertEqual(self.request('/api/workspaces/1/requirements',body)[0],403)
        self.assertEqual(self.request('/api/workspaces/1/requirements',body,{'X-CSRF-Token':self.csrf,'Origin':'https://evil.example'})[0],403)

    def test_http_scope_and_roles(self):
        self.assertEqual(self.request('/api/workspaces/2/state')[0],403)
        self.assertEqual(self.request('/api/workspaces/1/baselines',dict(name='forbidden',version_ids=[1]),{'X-CSRF-Token':self.csrf})[0],403)

    def test_http_create_and_read_history(self):
        status,result=self.request('/api/workspaces/1/requirements',{'content':content()},{'X-CSRF-Token':self.csrf})
        self.assertEqual(status,200)
        status,detail=self.request('/api/workspaces/1/requirements/'+str(result['id']))
        self.assertEqual(len(detail['versions']),1)

    def test_http_agent_run_is_recorded(self):
        status,result=self.request('/api/workspaces/1/agents/analyze',{'version_id':3,'mode':'rules'},{'X-CSRF-Token':self.csrf})
        self.assertEqual(status,200)
        self.assertEqual(len(result['events']),4)
        self.assertIn('run_id',result)

    def test_http_export_is_frozen_json(self):
        status,result=self.request('/api/workspaces/1/baselines/1/export')
        self.assertEqual(status,200)
        self.assertEqual(result['snapshot']['schema_version'],1)
        self.assertEqual(result['items'][0]['version_no'],1)

    def test_http_host_and_bad_password_are_rejected(self):
        self.assertEqual(self.request('/api/health',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/login',dict(login='analyst',password='wrong'))[0],401)


if __name__=='__main__':
    unittest.main(verbosity=2)
