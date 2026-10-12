PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS workspaces (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
 workspace_id INTEGER NOT NULL REFERENCES workspaces(id), user_id INTEGER NOT NULL REFERENCES users(id),
 role TEXT NOT NULL CHECK(role IN ('admin','analyst','reviewer','developer')), active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(workspace_id,user_id)
);
CREATE TABLE IF NOT EXISTS requirements (
 id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id), code TEXT NOT NULL,
 current_version_id INTEGER, owner_id INTEGER NOT NULL REFERENCES users(id), archived INTEGER NOT NULL DEFAULT 0,
 UNIQUE(workspace_id,code),
 FOREIGN KEY(current_version_id,id) REFERENCES requirement_versions(id,req_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS requirement_versions (
 id INTEGER PRIMARY KEY, req_id INTEGER NOT NULL REFERENCES requirements(id), version_no INTEGER NOT NULL,
 parent_req_id INTEGER REFERENCES requirements(id), title TEXT NOT NULL, body TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('functional','quality','constraint')),
 priority TEXT NOT NULL CHECK(priority IN ('Must','Should','Could')),
 source_ref TEXT NOT NULL DEFAULT '', rationale TEXT NOT NULL DEFAULT '', acceptance TEXT NOT NULL DEFAULT '',
 review_state TEXT NOT NULL CHECK(review_state IN ('draft','pending','approved','returned')),
 created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
 UNIQUE(req_id,version_no), UNIQUE(id,req_id), CHECK(parent_req_id IS NULL OR parent_req_id != req_id)
);
CREATE TABLE IF NOT EXISTS change_requests (
 id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id), reason TEXT NOT NULL,
 impact_summary TEXT NOT NULL DEFAULT '', requester_id INTEGER NOT NULL REFERENCES users(id),
 reviewer_id INTEGER REFERENCES users(id), status TEXT NOT NULL CHECK(status IN ('draft','pending','applied','rejected')),
 created_at TEXT NOT NULL, submitted_at TEXT, decided_at TEXT, comment TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS change_items (
 id INTEGER PRIMARY KEY, change_id INTEGER NOT NULL REFERENCES change_requests(id),
 req_id INTEGER NOT NULL REFERENCES requirements(id), base_version_id INTEGER NOT NULL,
 proposed_content TEXT NOT NULL, result_version_id INTEGER,
 UNIQUE(change_id,req_id), FOREIGN KEY(base_version_id,req_id) REFERENCES requirement_versions(id,req_id),
 FOREIGN KEY(result_version_id,req_id) REFERENCES requirement_versions(id,req_id)
);
CREATE TABLE IF NOT EXISTS reviews (
 id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES requirement_versions(id),
 submitted_by_id INTEGER NOT NULL REFERENCES users(id), reviewer_id INTEGER NOT NULL REFERENCES users(id),
 change_id INTEGER REFERENCES change_requests(id), decision TEXT NOT NULL CHECK(decision IN ('pending','approved','returned','withdrawn')),
 comment TEXT NOT NULL DEFAULT '', submitted_at TEXT NOT NULL, reviewed_at TEXT,
 CHECK(submitted_by_id != reviewer_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_review ON reviews(version_id) WHERE decision='pending';
CREATE TABLE IF NOT EXISTS artifacts (
 id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id),
 type TEXT NOT NULL CHECK(type IN ('design','verification')), external_key TEXT NOT NULL, title TEXT NOT NULL,
 version_ref TEXT NOT NULL, uri TEXT NOT NULL DEFAULT '', verification_method TEXT NOT NULL DEFAULT '',
 result TEXT NOT NULL CHECK(result IN ('not_run','passed','failed','not_applicable')),
 created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_links (
 id INTEGER PRIMARY KEY, source_version_id INTEGER NOT NULL REFERENCES requirement_versions(id),
 target_version_id INTEGER REFERENCES requirement_versions(id), artifact_id INTEGER REFERENCES artifacts(id),
 type TEXT NOT NULL CHECK(type IN ('derives_from','depends_on','conflicts_with','implemented_by','verified_by')),
 active INTEGER NOT NULL DEFAULT 1, suspect INTEGER NOT NULL DEFAULT 1,
 created_by INTEGER NOT NULL REFERENCES users(id), checked_by INTEGER REFERENCES users(id), checked_at TEXT,
 CHECK((target_version_id IS NOT NULL)+(artifact_id IS NOT NULL)=1),
 CHECK(target_version_id IS NULL OR target_version_id != source_version_id),
 CHECK((type IN ('derives_from','depends_on','conflicts_with') AND target_version_id IS NOT NULL)
 OR (type IN ('implemented_by','verified_by') AND artifact_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_active_requirement_link ON trace_links(source_version_id,target_version_id,type)
 WHERE active=1 AND target_version_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS unique_active_artifact_link ON trace_links(source_version_id,artifact_id,type)
 WHERE active=1 AND artifact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS trace_target ON trace_links(target_version_id);
CREATE TABLE IF NOT EXISTS baselines (
 id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id), name TEXT NOT NULL,
 created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, relation_snapshot TEXT NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0, revoke_reason TEXT NOT NULL DEFAULT '', UNIQUE(workspace_id,name)
);
CREATE TABLE IF NOT EXISTS baseline_items (
 baseline_id INTEGER NOT NULL REFERENCES baselines(id), req_id INTEGER NOT NULL REFERENCES requirements(id),
 version_id INTEGER NOT NULL, PRIMARY KEY(baseline_id,req_id),
 FOREIGN KEY(version_id,req_id) REFERENCES requirement_versions(id,req_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id), actor_id INTEGER NOT NULL REFERENCES users(id),
 action TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
 before_ref TEXT, after_ref TEXT, details TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), csrf TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
 id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL REFERENCES workspaces(id), version_id INTEGER NOT NULL REFERENCES requirement_versions(id),
 actor_id INTEGER NOT NULL REFERENCES users(id), mode TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS immutable_version_content BEFORE UPDATE OF req_id,version_no,parent_req_id,title,body,kind,priority,source_ref,rationale,acceptance,created_by,created_at
 ON requirement_versions BEGIN SELECT RAISE(ABORT,'version content is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_artifact BEFORE UPDATE ON artifacts BEGIN SELECT RAISE(ABORT,'artifact is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_baseline BEFORE UPDATE OF workspace_id,name,created_by,created_at,relation_snapshot ON baselines
 BEGIN SELECT RAISE(ABORT,'baseline snapshot is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_baseline_item_update BEFORE UPDATE ON baseline_items BEGIN SELECT RAISE(ABORT,'baseline item is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_baseline_item_delete BEFORE DELETE ON baseline_items BEGIN SELECT RAISE(ABORT,'baseline item is immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT,'audit is append only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_final_review BEFORE UPDATE ON reviews WHEN OLD.decision != 'pending'
 BEGIN SELECT RAISE(ABORT,'review decision is final'); END;
CREATE TRIGGER IF NOT EXISTS immutable_link_endpoints BEFORE UPDATE OF source_version_id,target_version_id,artifact_id,type,created_by ON trace_links
 BEGIN SELECT RAISE(ABORT,'link endpoints are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_submitted_change BEFORE UPDATE OF reason,impact_summary,requester_id,reviewer_id ON change_requests WHEN OLD.status != 'draft'
 BEGIN SELECT RAISE(ABORT,'submitted change is locked'); END;
CREATE TRIGGER IF NOT EXISTS immutable_submitted_items BEFORE UPDATE OF change_id,req_id,base_version_id,proposed_content ON change_items
 WHEN (SELECT status FROM change_requests WHERE id=OLD.change_id) != 'draft'
 BEGIN SELECT RAISE(ABORT,'submitted change item is locked'); END;
CREATE INDEX IF NOT EXISTS req_workspace ON requirements(workspace_id);
CREATE INDEX IF NOT EXISTS audit_workspace ON audit_events(workspace_id,id);
CREATE TRIGGER IF NOT EXISTS immutable_version_delete BEFORE DELETE ON requirement_versions BEGIN SELECT RAISE(ABORT,'version history cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS immutable_artifact_delete BEFORE DELETE ON artifacts BEGIN SELECT RAISE(ABORT,'artifact history cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS immutable_review_delete BEFORE DELETE ON reviews BEGIN SELECT RAISE(ABORT,'review history cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS immutable_baseline_delete BEFORE DELETE ON baselines BEGIN SELECT RAISE(ABORT,'baseline history cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS immutable_requirement_delete BEFORE DELETE ON requirements BEGIN SELECT RAISE(ABORT,'use archive instead of deleting requirements'); END;
CREATE TRIGGER IF NOT EXISTS locked_change_item_insert BEFORE INSERT ON change_items WHEN (SELECT status FROM change_requests WHERE id=NEW.change_id) != 'draft'
 BEGIN SELECT RAISE(ABORT,'submitted change is locked'); END;
CREATE TRIGGER IF NOT EXISTS locked_change_item_delete BEFORE DELETE ON change_items WHEN (SELECT status FROM change_requests WHERE id=OLD.change_id) != 'draft'
 BEGIN SELECT RAISE(ABORT,'submitted change is locked'); END;
