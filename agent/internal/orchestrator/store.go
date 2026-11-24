// Package orchestrator is the reviewer-v2 control plane: a durable task queue, the
// finding store, triggers from GitLab events, and the worker that runs the brain and
// posts its results (handoff M8).
package orchestrator

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	_ "github.com/lib/pq"
	_ "github.com/mattn/go-sqlite3"
)

// Task kinds.
const (
	KindReview = "review" // review an MR at its head
	KindThread = "thread" // answer a reply or re-check a finding
	KindPush   = "push"   // an MR got new commits: find the threads whose chain they touch
	KindMap    = "map"    // the cartographer: (re)write a project's repo card (background)
)

// Task statuses.
const (
	StatusQueued     = "queued"
	StatusRunning    = "running"
	StatusDone       = "done"
	StatusFailed     = "failed"
	StatusSuperseded = "superseded"
)

// Task is one unit of work in the durable queue.
type Task struct {
	ID          string
	Kind        string
	ProjectID   int64
	ProjectPath string
	MRIID       int64
	// ThreadKey serialises work: at most one running task per key, and a new task with
	// the same key supersedes a queued one (a newer event wins; a reply and a re-check
	// of the same thread never both answer).
	ThreadKey string
	Priority  int    // llmproxy priorities: reply 0 < recheck 1 < review 2 < background 3
	Event     string // for threads: reply | push
	Payload   map[string]any
	Status    string
	NotBefore time.Time
	CreatedAt time.Time
	Attempts  int
	Error     string
}

// StoredFinding is a posted finding with everything a later thread job needs.
type StoredFinding struct {
	ProjectID    int64
	MRIID        int64
	FindingID    string
	DiscussionID string
	NoteID       int64
	HeadSHA      string
	Status       string // open | fixed | withdrawn | unclear | ...
	Verdict      string // last thread verdict
	FindingJSON  json.RawMessage
	ChainPaths   []string // head-repository paths in the evidence chain
	Lesson       string
	UpdatedAt    time.Time
}

type dialect int

const (
	postgres dialect = iota
	sqlite
)

// Store is the durable state of the orchestrator, on Postgres (production) or SQLite.
type Store struct {
	db      *sql.DB
	dialect dialect
	now     func() time.Time
}

// Open connects and migrates. driver: "postgres" or "sqlite3".
func Open(driver, dsn string) (*Store, error) {
	var d dialect
	switch driver {
	case "postgres":
		d = postgres
	case "sqlite3", "sqlite":
		driver, d = "sqlite3", sqlite
		if !strings.Contains(dsn, "_txlock") {
			sep := "?"
			if strings.Contains(dsn, "?") {
				sep = "&"
			}
			dsn += sep + "_txlock=immediate&_busy_timeout=10000"
		}
	default:
		return nil, fmt.Errorf("unsupported driver %q", driver)
	}
	db, err := sql.Open(driver, dsn)
	if err != nil {
		return nil, err
	}
	if d == sqlite {
		db.SetMaxOpenConns(1)
	}
	s := &Store{db: db, dialect: d, now: time.Now}
	if err := s.migrate(context.Background()); err != nil {
		db.Close()
		return nil, err
	}
	return s, nil
}

func (s *Store) Close() error { return s.db.Close() }

func (s *Store) rebind(q string) string {
	if s.dialect != postgres {
		return q
	}
	var b strings.Builder
	n := 0
	for _, r := range q {
		if r == '?' {
			n++
			fmt.Fprintf(&b, "$%d", n)
			continue
		}
		b.WriteRune(r)
	}
	return b.String()
}

func (s *Store) exec(ctx context.Context, tx *sql.Tx, q string, args ...any) (sql.Result, error) {
	if tx != nil {
		return tx.ExecContext(ctx, s.rebind(q), args...)
	}
	return s.db.ExecContext(ctx, s.rebind(q), args...)
}

func (s *Store) migrate(ctx context.Context) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS hbk_tasks (
			id TEXT PRIMARY KEY,
			kind TEXT NOT NULL,
			project_id BIGINT NOT NULL,
			project_path TEXT NOT NULL DEFAULT '',
			mr_iid BIGINT NOT NULL,
			thread_key TEXT NOT NULL DEFAULT '',
			priority INTEGER NOT NULL,
			event TEXT NOT NULL DEFAULT '',
			payload TEXT NOT NULL DEFAULT '{}',
			status TEXT NOT NULL,
			not_before BIGINT NOT NULL,
			created_at BIGINT NOT NULL,
			updated_at BIGINT NOT NULL,
			lease_until BIGINT NOT NULL DEFAULT 0,
			worker TEXT NOT NULL DEFAULT '',
			attempts INTEGER NOT NULL DEFAULT 0,
			error TEXT NOT NULL DEFAULT ''
		)`,
		`CREATE INDEX IF NOT EXISTS hbk_tasks_ready ON hbk_tasks (status, priority, created_at)`,
		`CREATE INDEX IF NOT EXISTS hbk_tasks_thread ON hbk_tasks (thread_key, status)`,
		`CREATE TABLE IF NOT EXISTS hbk_findings (
			project_id BIGINT NOT NULL,
			mr_iid BIGINT NOT NULL,
			finding_id TEXT NOT NULL,
			discussion_id TEXT NOT NULL DEFAULT '',
			note_id BIGINT NOT NULL DEFAULT 0,
			head_sha TEXT NOT NULL,
			status TEXT NOT NULL,
			verdict TEXT NOT NULL DEFAULT '',
			finding_json TEXT NOT NULL,
			chain_paths TEXT NOT NULL DEFAULT '[]',
			lesson TEXT NOT NULL DEFAULT '',
			created_at BIGINT NOT NULL,
			updated_at BIGINT NOT NULL,
			PRIMARY KEY (project_id, mr_iid, finding_id)
		)`,
		`CREATE INDEX IF NOT EXISTS hbk_findings_discussion ON hbk_findings (discussion_id)`,
	}
	for _, stmt := range stmts {
		if _, err := s.db.ExecContext(ctx, stmt); err != nil {
			return fmt.Errorf("migrate: %w", err)
		}
	}
	return nil
}

func ms(t time.Time) int64 { return t.UnixMilli() }

func (s *Store) begin(ctx context.Context) (*sql.Tx, error) {
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	if s.dialect == postgres {
		// Claims and supersedes are rare (dozens an hour): serialise them simply.
		if _, err := tx.ExecContext(ctx, "LOCK TABLE hbk_tasks IN SHARE ROW EXCLUSIVE MODE"); err != nil {
			tx.Rollback()
			return nil, err
		}
	}
	return tx, nil
}

// Enqueue adds a task. A queued task with the same thread key is superseded by it;
// if either was a reply, the merged task is a reply (higher priority, and the brain
// sees the push in its diff anyway). Returns the stored task.
func (s *Store) Enqueue(ctx context.Context, t Task) (*Task, error) {
	now := s.now()
	if t.ID == "" {
		t.ID = uuid.NewString()
	}
	if t.NotBefore.IsZero() {
		t.NotBefore = now
	}
	t.Status, t.CreatedAt = StatusQueued, now
	tx, err := s.begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	if t.ThreadKey != "" {
		rows, err := tx.QueryContext(ctx, s.rebind(
			`SELECT id, event, priority FROM hbk_tasks WHERE thread_key = ? AND status = ?`),
			t.ThreadKey, StatusQueued)
		if err != nil {
			return nil, err
		}
		var old []string
		for rows.Next() {
			var id, event string
			var prio int
			if err := rows.Scan(&id, &event, &prio); err != nil {
				rows.Close()
				return nil, err
			}
			old = append(old, id)
			if event == "reply" {
				t.Event = "reply"
			}
			if prio < t.Priority {
				t.Priority = prio
			}
		}
		rows.Close()
		for _, id := range old {
			if _, err := s.exec(ctx, tx, `UPDATE hbk_tasks SET status = ?, updated_at = ?, error = ? WHERE id = ?`,
				StatusSuperseded, ms(now), "superseded by "+t.ID, id); err != nil {
				return nil, err
			}
		}
	}
	payload, _ := json.Marshal(t.Payload)
	if t.Payload == nil {
		payload = []byte("{}")
	}
	if _, err := s.exec(ctx, tx, `INSERT INTO hbk_tasks (id, kind, project_id, project_path, mr_iid,
		thread_key, priority, event, payload, status, not_before, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		t.ID, t.Kind, t.ProjectID, t.ProjectPath, t.MRIID, t.ThreadKey, t.Priority, t.Event,
		string(payload), t.Status, ms(t.NotBefore), ms(now), ms(now)); err != nil {
		return nil, err
	}
	return &t, tx.Commit()
}

// Claim takes the next ready task: highest priority, then oldest, whose debounce has
// passed and whose thread key has nothing running. Returns nil when none is ready.
func (s *Store) Claim(ctx context.Context, worker string, lease time.Duration) (*Task, error) {
	now := s.now()
	tx, err := s.begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	row := tx.QueryRowContext(ctx, s.rebind(`SELECT id, kind, project_id, project_path, mr_iid,
		thread_key, priority, event, payload, not_before, created_at, attempts
		FROM hbk_tasks t WHERE t.status = ? AND t.not_before <= ?
		AND (t.thread_key = '' OR NOT EXISTS (SELECT 1 FROM hbk_tasks r
			WHERE r.thread_key = t.thread_key AND r.status = ?))
		ORDER BY t.priority, t.created_at LIMIT 1`), StatusQueued, ms(now), StatusRunning)
	var t Task
	var payload string
	var notBefore, created int64
	if err := row.Scan(&t.ID, &t.Kind, &t.ProjectID, &t.ProjectPath, &t.MRIID, &t.ThreadKey,
		&t.Priority, &t.Event, &payload, &notBefore, &created, &t.Attempts); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	if _, err := s.exec(ctx, tx, `UPDATE hbk_tasks SET status = ?, worker = ?, lease_until = ?,
		attempts = attempts + 1, updated_at = ? WHERE id = ?`,
		StatusRunning, worker, ms(now.Add(lease)), ms(now), t.ID); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(payload), &t.Payload)
	t.Status, t.Attempts = StatusRunning, t.Attempts+1
	t.NotBefore, t.CreatedAt = time.UnixMilli(notBefore), time.UnixMilli(created)
	return &t, nil
}

// Heartbeat extends a running task's lease.
func (s *Store) Heartbeat(ctx context.Context, id string, lease time.Duration) error {
	_, err := s.exec(ctx, nil, `UPDATE hbk_tasks SET lease_until = ? WHERE id = ? AND status = ?`,
		ms(s.now().Add(lease)), id, StatusRunning)
	return err
}

// Finish records a task's outcome.
func (s *Store) Finish(ctx context.Context, id, status, errMsg string) error {
	_, err := s.exec(ctx, nil, `UPDATE hbk_tasks SET status = ?, error = ?, updated_at = ?, lease_until = 0
		WHERE id = ?`, status, errMsg, ms(s.now()), id)
	return err
}

// RecoverExpired puts tasks whose worker died (lease expired) back in the queue, or
// fails them after maxAttempts. Returns how many it touched.
func (s *Store) RecoverExpired(ctx context.Context, maxAttempts int) (int, error) {
	now := ms(s.now())
	res, err := s.exec(ctx, nil, `UPDATE hbk_tasks SET status = CASE WHEN attempts >= ? THEN ? ELSE ? END,
		error = CASE WHEN attempts >= ? THEN 'lease expired too often' ELSE error END,
		updated_at = ?, lease_until = 0 WHERE status = ? AND lease_until < ?`,
		maxAttempts, StatusFailed, StatusQueued, maxAttempts, now, StatusRunning, now)
	if err != nil {
		return 0, err
	}
	n, _ := res.RowsAffected()
	return int(n), nil
}

// Get returns a task by id.
func (s *Store) Get(ctx context.Context, id string) (*Task, error) {
	row := s.db.QueryRowContext(ctx, s.rebind(`SELECT id, kind, project_id, mr_iid, thread_key,
		priority, event, status, attempts, error FROM hbk_tasks WHERE id = ?`), id)
	var t Task
	if err := row.Scan(&t.ID, &t.Kind, &t.ProjectID, &t.MRIID, &t.ThreadKey, &t.Priority,
		&t.Event, &t.Status, &t.Attempts, &t.Error); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		return nil, err
	}
	return &t, nil
}

// QueuePosition is how many ready tasks would run before this one ("queued, position 3").
func (s *Store) QueuePosition(ctx context.Context, t *Task) (int, error) {
	row := s.db.QueryRowContext(ctx, s.rebind(`SELECT COUNT(*) FROM hbk_tasks WHERE status IN (?, ?)
		AND (priority < ? OR (priority = ? AND created_at < ?))`),
		StatusQueued, StatusRunning, t.Priority, t.Priority, ms(t.CreatedAt))
	var n int
	return n, row.Scan(&n)
}

// ─── findings ──────────────────────────────────────────────────────────────────

// SaveFinding inserts or updates a posted finding.
func (s *Store) SaveFinding(ctx context.Context, f StoredFinding) error {
	now := ms(s.now())
	paths, _ := json.Marshal(f.ChainPaths)
	if f.Status == "" {
		f.Status = "open"
	}
	_, err := s.exec(ctx, nil, `INSERT INTO hbk_findings (project_id, mr_iid, finding_id, discussion_id,
		note_id, head_sha, status, verdict, finding_json, chain_paths, lesson, created_at, updated_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT (project_id, mr_iid, finding_id) DO UPDATE SET discussion_id = excluded.discussion_id,
		note_id = excluded.note_id, head_sha = excluded.head_sha, status = excluded.status,
		verdict = excluded.verdict, finding_json = excluded.finding_json,
		chain_paths = excluded.chain_paths, lesson = excluded.lesson, updated_at = excluded.updated_at`,
		f.ProjectID, f.MRIID, f.FindingID, f.DiscussionID, f.NoteID, f.HeadSHA, f.Status, f.Verdict,
		string(f.FindingJSON), string(paths), f.Lesson, now, now)
	return err
}

func (s *Store) scanFindings(rows *sql.Rows) ([]StoredFinding, error) {
	defer rows.Close()
	var out []StoredFinding
	for rows.Next() {
		var f StoredFinding
		var body, paths string
		var updated int64
		if err := rows.Scan(&f.ProjectID, &f.MRIID, &f.FindingID, &f.DiscussionID, &f.NoteID,
			&f.HeadSHA, &f.Status, &f.Verdict, &body, &paths, &f.Lesson, &updated); err != nil {
			return nil, err
		}
		f.FindingJSON = json.RawMessage(body)
		_ = json.Unmarshal([]byte(paths), &f.ChainPaths)
		f.UpdatedAt = time.UnixMilli(updated)
		out = append(out, f)
	}
	return out, rows.Err()
}

const findingCols = `project_id, mr_iid, finding_id, discussion_id, note_id, head_sha, status, verdict,
	finding_json, chain_paths, lesson, updated_at`

// FindingByDiscussion returns the finding a bot thread was opened for, or nil.
func (s *Store) FindingByDiscussion(ctx context.Context, projectID, mrIID int64, discussionID string) (*StoredFinding, error) {
	rows, err := s.db.QueryContext(ctx, s.rebind(`SELECT `+findingCols+` FROM hbk_findings
		WHERE project_id = ? AND mr_iid = ? AND discussion_id = ?`), projectID, mrIID, discussionID)
	if err != nil {
		return nil, err
	}
	found, err := s.scanFindings(rows)
	if err != nil || len(found) == 0 {
		return nil, err
	}
	return &found[0], nil
}

// Findings lists an MR's findings; open=true keeps only those still open.
func (s *Store) Findings(ctx context.Context, projectID, mrIID int64, open bool) ([]StoredFinding, error) {
	q := `SELECT ` + findingCols + ` FROM hbk_findings WHERE project_id = ? AND mr_iid = ?`
	args := []any{projectID, mrIID}
	if open {
		q += ` AND status IN ('open', 'unclear', 'need_info')`
	}
	rows, err := s.db.QueryContext(ctx, s.rebind(q+` ORDER BY created_at`), args...)
	if err != nil {
		return nil, err
	}
	return s.scanFindings(rows)
}

// RecordVerdict stores a thread verdict on a finding. fixed and withdrawn close it
// (the thread itself is never resolved by the bot).
func (s *Store) RecordVerdict(ctx context.Context, projectID, mrIID int64, findingID, verdict, lesson string) error {
	status := "open"
	switch verdict {
	case "fixed", "withdrawn":
		status = verdict
	case "unclear", "need_info":
		status = verdict
	}
	_, err := s.exec(ctx, nil, `UPDATE hbk_findings SET verdict = ?, status = ?, lesson = CASE WHEN ? = ''
		THEN lesson ELSE ? END, updated_at = ? WHERE project_id = ? AND mr_iid = ? AND finding_id = ?`,
		verdict, status, lesson, lesson, ms(s.now()), projectID, mrIID, findingID)
	return err
}
