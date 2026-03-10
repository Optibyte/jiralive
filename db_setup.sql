-- Jira Database Setup Script - V3 (Enriched Raw Events + RBAC)

-- 1. Projects Table
DROP TABLE IF EXISTS projects CASCADE;
CREATE TABLE projects (
    id VARCHAR PRIMARY KEY,
    key VARCHAR(50),
    name TEXT,
    project_type VARCHAR(50),
    lead_account_id VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. Users Table
DROP TABLE IF EXISTS users CASCADE;
CREATE TABLE users (
    account_id VARCHAR PRIMARY KEY,
    display_name TEXT,
    email TEXT,
    active BOOLEAN,
    avatar_url TEXT
);

-- 3. Issues Table
DROP TABLE IF EXISTS issues CASCADE;
CREATE TABLE issues (
    id VARCHAR PRIMARY KEY,
    issue_key VARCHAR(50) UNIQUE,
    project_id VARCHAR,
    summary TEXT,
    description TEXT,
    issue_type VARCHAR(50),
    status VARCHAR(50),
    priority VARCHAR(50),
    reporter VARCHAR,
    assignee VARCHAR,
    story_points INTEGER DEFAULT 0,
    planned_story_points INTEGER DEFAULT 0,
    is_client_reported BOOLEAN DEFAULT FALSE,
    qa_defects_count INTEGER DEFAULT 0,
    review_comments_count INTEGER DEFAULT 0,
    created TIMESTAMP,
    updated TIMESTAMP,
    due_date DATE,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (assignee) REFERENCES users(account_id),
    FOREIGN KEY (reporter) REFERENCES users(account_id)
);

-- 4. Issue Status History
DROP TABLE IF EXISTS issue_status_history CASCADE;
CREATE TABLE issue_status_history (
    id SERIAL PRIMARY KEY,
    issue_id VARCHAR,
    from_status VARCHAR,
    to_status VARCHAR,
    changed_by VARCHAR,
    changed_at TIMESTAMP,
    FOREIGN KEY (issue_id) REFERENCES issues(id)
);

-- 5. Comments Table
DROP TABLE IF EXISTS comments CASCADE;
CREATE TABLE comments (
    id VARCHAR PRIMARY KEY,
    issue_id VARCHAR,
    author_account_id VARCHAR,
    body TEXT,
    created TIMESTAMP,
    updated TIMESTAMP,
    FOREIGN KEY (issue_id) REFERENCES issues(id),
    FOREIGN KEY (author_account_id) REFERENCES users(account_id)
);

-- 6. Worklogs Table
DROP TABLE IF EXISTS worklogs CASCADE;
CREATE TABLE worklogs (
    id VARCHAR PRIMARY KEY,
    issue_id VARCHAR,
    author_account_id VARCHAR,
    time_spent_seconds INTEGER,
    started TIMESTAMP,
    created TIMESTAMP,
    FOREIGN KEY (issue_id) REFERENCES issues(id),
    FOREIGN KEY (author_account_id) REFERENCES users(account_id)
);

-- 7. Sprints Table
DROP TABLE IF EXISTS sprints CASCADE;
CREATE TABLE sprints (
    id VARCHAR PRIMARY KEY,
    name TEXT,
    state VARCHAR,
    start_date TIMESTAMP,
    end_date TIMESTAMP,
    board_id VARCHAR,
    capacity INTEGER DEFAULT 100
);

-- 8. Issue Sprint Mapping
DROP TABLE IF EXISTS issue_sprints CASCADE;
CREATE TABLE issue_sprints (
    issue_id VARCHAR,
    sprint_id VARCHAR,
    PRIMARY KEY(issue_id, sprint_id),
    FOREIGN KEY (issue_id) REFERENCES issues(id),
    FOREIGN KEY (sprint_id) REFERENCES sprints(id)
);

-- 9. ENRICHED Webhook Raw Data Table (V3 - NEW ARCHITECTURE)
-- This table stores ALL extracted IDs from Jira events, making it queryable by RBAC dimensions
DROP TABLE IF EXISTS jira_webhook_raw CASCADE;
CREATE TABLE jira_webhook_raw (
  -- Primary identity
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id         VARCHAR(255) UNIQUE,
  event_type       VARCHAR(100) NOT NULL,
  received_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  -- Jira-side identifiers (extracted from payload)
  jira_issue_id    VARCHAR(100),
  jira_issue_key   VARCHAR(50),
  jira_project_id  VARCHAR(100),
  jira_project_key VARCHAR(50),
  jira_board_id    VARCHAR(100),
  jira_sprint_id   VARCHAR(100),
  jira_sprint_name VARCHAR(255),

  -- CTO Platform identifiers (resolved via mapping tables)
  cto_project_id   UUID,
  cto_account_id   UUID,
  cto_market_id    UUID,
  cto_org_id       UUID,
  cto_team_id      UUID,
  cto_user_id      UUID,

  -- User / assignee details (extracted)
  jira_assignee_account_id  VARCHAR(100),
  jira_reporter_account_id  VARCHAR(100),
  jira_creator_account_id   VARCHAR(100),

  -- Issue attributes (extracted for fast aggregation)
  issue_type       VARCHAR(100),
  issue_status     VARCHAR(100),
  issue_priority   VARCHAR(50),
  story_points     DECIMAL(10,2),
  labels           TEXT[],

  -- Sprint / timing
  sprint_state     VARCHAR(50),
  sprint_start_date TIMESTAMPTZ,
  sprint_end_date   TIMESTAMPTZ,
  issue_created_at  TIMESTAMPTZ,
  issue_updated_at  TIMESTAMPTZ,
  issue_resolved_at TIMESTAMPTZ,

  -- Change tracking (for status transitions)
  changed_field    VARCHAR(100),
  old_value        TEXT,
  new_value        TEXT,

  -- Full payload preserved
  raw_payload      JSONB NOT NULL,
  processing_status VARCHAR(20) DEFAULT 'stored'
);

-- Create indexes for fast RBAC queries
CREATE INDEX idx_raw_cto_project   ON jira_webhook_raw(cto_project_id);
CREATE INDEX idx_raw_cto_account   ON jira_webhook_raw(cto_account_id);
CREATE INDEX idx_raw_cto_market    ON jira_webhook_raw(cto_market_id);
CREATE INDEX idx_raw_cto_team      ON jira_webhook_raw(cto_team_id);
CREATE INDEX idx_raw_cto_user      ON jira_webhook_raw(cto_user_id);
CREATE INDEX idx_raw_received_at   ON jira_webhook_raw(received_at DESC);
CREATE INDEX idx_raw_event_type    ON jira_webhook_raw(event_type);
CREATE INDEX idx_raw_issue_status  ON jira_webhook_raw(issue_status);
CREATE INDEX idx_raw_composite     ON jira_webhook_raw(cto_project_id, received_at DESC);
