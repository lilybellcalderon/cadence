-- Cadence schema v2.  Timestamps are ISO-8601 UTC text ("2026-10-26T14:03:09Z") so they sort lexically.
-- JSON columns are TEXT guarded by json_valid().  Invariants from DESIGN.md that the database enforces itself:
--   * a task has at most ONE active assignee            (ux_one_active_assignment_per_task)
--   * a task has at most ONE pending routing request    (ux_one_pending_request_per_task)  -> no double-submit on restart
--   * events and estimate revisions are append-only     (triggers)

-- ---------------------------------------------------------------- reference data
CREATE TABLE expertise_levels (
    level   INTEGER PRIMARY KEY CHECK (level BETWEEN 1 AND 4),
    name    TEXT NOT NULL UNIQUE,
    meaning TEXT NOT NULL
);

CREATE TABLE expertise_areas (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    summary              TEXT NOT NULL,
    description          TEXT NOT NULL,
    in_scope             TEXT NOT NULL CHECK (json_valid(in_scope)),
    out_of_scope         TEXT NOT NULL CHECK (json_valid(out_of_scope)),
    level_rubric         TEXT NOT NULL CHECK (json_valid(level_rubric)),
    typical_deliverables TEXT NOT NULL CHECK (json_valid(typical_deliverables)),
    example_tasks        TEXT NOT NULL CHECK (json_valid(example_tasks)),
    keywords             TEXT NOT NULL CHECK (json_valid(keywords))
);

CREATE TABLE area_adjacency (
    area_id          TEXT NOT NULL REFERENCES expertise_areas(id),
    adjacent_area_id TEXT NOT NULL REFERENCES expertise_areas(id),
    PRIMARY KEY (area_id, adjacent_area_id),
    CHECK (area_id <> adjacent_area_id)
);

CREATE TABLE area_sub_skills (
    area_id     TEXT NOT NULL REFERENCES expertise_areas(id),
    id          TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY (area_id, id)
);

CREATE TABLE contributors (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    title                TEXT NOT NULL,
    email                TEXT NOT NULL UNIQUE,
    timezone             TEXT NOT NULL,
    availability         TEXT NOT NULL CHECK (availability IN ('available', 'away', 'offline')),
    away_reason          TEXT,
    max_concurrent_tasks INTEGER NOT NULL CHECK (max_concurrent_tasks > 0),
    available_hours      REAL NOT NULL CHECK (available_hours > 0),
    years_experience     INTEGER,
    bio                  TEXT
);

CREATE TABLE contributor_expertise (
    contributor_id TEXT NOT NULL REFERENCES contributors(id),
    area_id        TEXT NOT NULL REFERENCES expertise_areas(id),
    level          INTEGER NOT NULL REFERENCES expertise_levels(level),
    PRIMARY KEY (contributor_id, area_id)
);

CREATE TABLE contributor_skills (
    contributor_id TEXT NOT NULL,
    area_id        TEXT NOT NULL,
    sub_skill_id   TEXT NOT NULL,
    PRIMARY KEY (contributor_id, area_id, sub_skill_id),
    FOREIGN KEY (contributor_id, area_id) REFERENCES contributor_expertise(contributor_id, area_id),
    FOREIGN KEY (area_id, sub_skill_id)   REFERENCES area_sub_skills(area_id, id)
);

-- ---------------------------------------------------------------- request repository
CREATE TABLE tasks (
    id                     TEXT PRIMARY KEY,
    title                  TEXT NOT NULL,
    area_id                TEXT NOT NULL REFERENCES expertise_areas(id),
    min_level              INTEGER NOT NULL REFERENCES expertise_levels(level),
    priority               TEXT NOT NULL CHECK (priority IN ('critical', 'high', 'medium', 'low')),
    status                 TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                               'draft', 'submitted', 'routing', 'queued', 'unmatched', 'offered',
                               'accepted', 'in_progress', 'completed', 'declined', 'failed', 'cancelled')),
    requester_name         TEXT NOT NULL,
    requester_team         TEXT NOT NULL,
    story_as_a             TEXT NOT NULL,
    story_i_want           TEXT NOT NULL,
    story_so_that          TEXT NOT NULL,
    description            TEXT NOT NULL,
    estimated_effort_hours REAL NOT NULL CHECK (estimated_effort_hours > 0),  -- requester's PROVISIONAL estimate
    tags                   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags)),
    sim                    TEXT CHECK (sim IS NULL OR json_valid(sim)),        -- scripted demo behaviour; NULL = default engine
    created_at             TEXT NOT NULL,
    due_at                 TEXT,
    updated_at             TEXT NOT NULL,
    parent_task_id         TEXT REFERENCES tasks(id),   -- set on the children created when an operator splits a task
    hold_reason            TEXT                         -- 'availability' = unmatched and waiting for an eligible contributor
);

CREATE TABLE task_attachments (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    kind        TEXT NOT NULL CHECK (kind IN ('requirements', 'acceptance_criteria', 'sample_data', 'reference')),
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,          -- relative to the data/ folder
    description TEXT,
    size_bytes  INTEGER NOT NULL,
    sha256      TEXT NOT NULL
);

-- ---------------------------------------------------------------- routing
CREATE TABLE routing_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL REFERENCES tasks(id),
    attempt          INTEGER NOT NULL,
    started_at       TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'returned', 'superseded', 'abandoned')),
    eligible         TEXT NOT NULL CHECK (json_valid(eligible)),     -- snapshot handed to the engine
    -- The simulated outcome is decided when the request is created and stored here, so a restart
    -- re-arms the same behaviour instead of re-rolling the dice.  NULL returns_at = never returns.
    sim_returns_at   TEXT,
    sim_pick         TEXT REFERENCES contributors(id),               -- NULL with a returns_at = "no match found"
    stuck_flagged_at TEXT,                                           -- last time a stuck alert was raised for this request
    stuck_alerts     INTEGER NOT NULL DEFAULT 0,
    late_returned_at TEXT,                                           -- a superseded request whose result arrived afterwards (logged, ignored)
    finished_at      TEXT,
    result           TEXT CHECK (result IS NULL OR json_valid(result)),
    UNIQUE (task_id, attempt)
);
-- a late result is matched by request id; only one request per task may be live at a time
CREATE UNIQUE INDEX ux_one_pending_request_per_task ON routing_requests(task_id) WHERE status = 'pending';

CREATE TABLE routing_exclusions (          -- contributors the operator said not to re-offer this task to
    task_id        TEXT NOT NULL REFERENCES tasks(id),
    contributor_id TEXT NOT NULL REFERENCES contributors(id),
    reason         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (task_id, contributor_id)
);

CREATE TABLE assignments (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                TEXT NOT NULL REFERENCES tasks(id),
    contributor_id         TEXT NOT NULL REFERENCES contributors(id),
    routing_request_id     INTEGER REFERENCES routing_requests(id),
    source                 TEXT NOT NULL CHECK (source IN ('routing', 'operator')),
    status                 TEXT NOT NULL DEFAULT 'offered' CHECK (status IN (
                               'offered', 'accepted', 'in_progress', 'completed', 'declined', 'failed', 'cancelled')),
    offered_at             TEXT NOT NULL,
    decided_at             TEXT,
    started_at             TEXT,
    completed_at           TEXT,
    end_reason             TEXT,
    estimate_due_at        TEXT,       -- set when accepted/assigned, from the priority SLA
    plan_health            TEXT NOT NULL DEFAULT 'awaiting_estimate' CHECK (plan_health IN (
                               'complete', 'awaiting_estimate', 'overdue_estimate', 'stale', 'at_risk')),
    plan_health_updated_at TEXT
);
-- DESIGN: "each task can have at most one active assignee"
CREATE UNIQUE INDEX ux_one_active_assignment_per_task ON assignments(task_id)
    WHERE status IN ('offered', 'accepted', 'in_progress');

-- ---------------------------------------------------------------- TrackingAgent data
CREATE TABLE estimate_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id   INTEGER NOT NULL REFERENCES assignments(id),
    revision        INTEGER NOT NULL,
    hours_total     REAL NOT NULL CHECK (hours_total > 0),
    hours_remaining REAL NOT NULL CHECK (hours_remaining >= 0),    -- may exceed hours_total (scope growth)
    confidence      TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    basis           TEXT NOT NULL CHECK (basis IN ('similar_work', 'spec_review', 'rough_guess')),
    source          TEXT NOT NULL CHECK (source IN ('contributor', 'check_in', 'operator_on_behalf')),
    submitted_by    TEXT NOT NULL,
    submitted_at    TEXT NOT NULL,
    note            TEXT,
    UNIQUE (assignment_id, revision)
);

CREATE TABLE estimate_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    kind          TEXT NOT NULL CHECK (kind IN ('request', 'reminder')),
    sequence      INTEGER NOT NULL,                 -- 0 = initial request, 1..n = reminders
    sent_at       TEXT NOT NULL,
    due_at        TEXT NOT NULL,
    answered_at   TEXT,
    UNIQUE (assignment_id, sequence)
);

CREATE TABLE status_checks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id    INTEGER NOT NULL REFERENCES assignments(id),
    sent_at          TEXT NOT NULL,
    due_at           TEXT NOT NULL,
    replied_at       TEXT,
    status           TEXT CHECK (status IS NULL OR status IN ('on_track', 'at_risk', 'blocked', 'no_response')),
    progress_percent INTEGER CHECK (progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100),
    hours_remaining  REAL CHECK (hours_remaining IS NULL OR hours_remaining >= 0),
    note             TEXT
);

-- ---------------------------------------------------------------- history, alerts, questions
CREATE TABLE events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    task_id       TEXT REFERENCES tasks(id),
    assignment_id INTEGER REFERENCES assignments(id),
    event_type    TEXT NOT NULL,
    actor         TEXT NOT NULL,              -- seed | system | RoutingAgent | TrackingAgent | operator:<name> | contributor:<id>
    payload       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload))
);
CREATE INDEX ix_events_task ON events(task_id, id);

CREATE TABLE alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT REFERENCES tasks(id),
    assignment_id    INTEGER REFERENCES assignments(id),
    trigger          TEXT NOT NULL,
    agent            TEXT NOT NULL CHECK (agent IN ('RoutingAgent', 'TrackingAgent')),
    severity         TEXT NOT NULL CHECK (severity IN ('info', 'low', 'normal', 'high', 'critical')),
    message          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    dedupe_key       TEXT NOT NULL,           -- e.g. "stuck_request:T-1002:3"; stops sweeps from raising the same alert repeatedly
    created_at       TEXT NOT NULL,
    acknowledged_at  TEXT,
    acknowledged_by  TEXT,
    resolved_at      TEXT,
    re_alert_count   INTEGER NOT NULL DEFAULT 0,
    next_re_alert_at TEXT
);
CREATE UNIQUE INDEX ux_one_unresolved_alert_per_key ON alerts(dedupe_key) WHERE status <> 'resolved';
CREATE INDEX ix_alerts_open ON alerts(status, severity);

CREATE TABLE operator_questions (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id                   INTEGER REFERENCES alerts(id),
    task_id                    TEXT REFERENCES tasks(id),
    type                       TEXT NOT NULL CHECK (type IN (
                                   'stuck_request', 'no_match', 'capacity_conflict', 'priority_contention', 'recovery',
                                   'estimate_missing', 'estimate_variance', 'contributor_silent', 'at_risk')),
    prompt                     TEXT NOT NULL,
    context                    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(context)),   -- what the agent knew when it asked
    options                    TEXT NOT NULL CHECK (json_valid(options)),
    safe_default               TEXT NOT NULL,
    status                     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'withdrawn')),
    asked_at                   TEXT NOT NULL,
    answer_due_at              TEXT NOT NULL,
    answered_at                TEXT,
    answered_by                TEXT,
    answer                     TEXT,
    note                       TEXT,
    safe_default_applied_count INTEGER NOT NULL DEFAULT 0,
    last_safe_default_at       TEXT
);
CREATE INDEX ix_questions_open ON operator_questions(status, answer_due_at);

CREATE TABLE outbox (                          -- simulated external channel (email/chat) for critical + high alerts
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id     INTEGER REFERENCES alerts(id),
    channel      TEXT NOT NULL,
    recipient    TEXT NOT NULL DEFAULT 'operator',
    subject      TEXT NOT NULL,
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE daily_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date  TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    content      TEXT NOT NULL CHECK (json_valid(content))
);

-- ---------------------------------------------------------------- configuration in force
CREATE TABLE config_snapshots (               -- which policy was live when decisions were made
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256    TEXT NOT NULL UNIQUE,
    loaded_at TEXT NOT NULL,
    config    TEXT NOT NULL CHECK (json_valid(config))
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- ---------------------------------------------------------------- append-only guards
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER estimate_revisions_no_update BEFORE UPDATE ON estimate_revisions
BEGIN SELECT RAISE(ABORT, 'estimate_revisions is append-only'); END;
CREATE TRIGGER estimate_revisions_no_delete BEFORE DELETE ON estimate_revisions
BEGIN SELECT RAISE(ABORT, 'estimate_revisions is append-only'); END;

-- ---------------------------------------------------------------- views the agents read
-- Eligibility (hard rule): right area at >= min level, and declared available.  Capacity is NOT part of it.
CREATE VIEW v_eligible_contributors AS
SELECT t.id AS task_id, c.id AS contributor_id, ce.level AS level
FROM tasks t
JOIN contributor_expertise ce ON ce.area_id = t.area_id AND ce.level >= t.min_level
JOIN contributors c ON c.id = ce.contributor_id AND c.availability = 'available';

-- Hours each active assignment currently counts for (DESIGN section 9.1):
--   latest estimate revision (contributor estimate or check-in remaining), else the requester's provisional
--   estimate x settings.unconfirmed_buffer, labelled 'requester_unconfirmed'.
CREATE VIEW v_assignment_load AS
SELECT a.id AS assignment_id, a.task_id, a.contributor_id, a.plan_health,
       COALESCE(er.hours_remaining,
                t.estimated_effort_hours * COALESCE((SELECT CAST(value AS REAL) FROM settings WHERE key = 'unconfirmed_buffer'), 1.25)
       ) AS load_hours,
       CASE WHEN er.id IS NULL THEN 'requester_unconfirmed' ELSE 'revision:' || er.source END AS load_basis
FROM assignments a
JOIN tasks t ON t.id = a.task_id
LEFT JOIN estimate_revisions er ON er.assignment_id = a.id
     AND er.revision = (SELECT MAX(revision) FROM estimate_revisions WHERE assignment_id = a.id)
WHERE a.status IN ('offered', 'accepted', 'in_progress');

-- Per-contributor load vs. limits: what the RoutingAgent compares a new task against.
CREATE VIEW v_contributor_load AS
SELECT c.id AS contributor_id, c.max_concurrent_tasks, c.available_hours,
       COUNT(l.assignment_id)                                              AS active_tasks,
       COALESCE(SUM(l.load_hours), 0)                                      AS load_hours,
       COALESCE(SUM(l.load_basis = 'requester_unconfirmed'), 0)            AS unconfirmed_assignments,
       COALESCE(SUM(l.plan_health = 'overdue_estimate'), 0)                AS overdue_estimates
FROM contributors c
LEFT JOIN v_assignment_load l ON l.contributor_id = c.id
GROUP BY c.id;
