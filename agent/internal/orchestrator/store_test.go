package orchestrator

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// stores returns a fresh SQLite store, and a Postgres one when HABODEK_TEST_POSTGRES
// holds a DSN (CI and the VPN run both; the Postgres path is the production one).
func stores(t *testing.T) map[string]*Store {
	t.Helper()
	out := map[string]*Store{}
	lite, err := Open("sqlite3", filepath.Join(t.TempDir(), "hbk.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { lite.Close() })
	out["sqlite"] = lite
	if dsn := os.Getenv("HABODEK_TEST_POSTGRES"); dsn != "" {
		admin, err := sql.Open("postgres", dsn)
		if err != nil {
			t.Fatal(err)
		}
		schema := fmt.Sprintf("t%d", time.Now().UnixNano())
		if _, err := admin.Exec("CREATE SCHEMA " + schema); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { admin.Exec("DROP SCHEMA " + schema + " CASCADE"); admin.Close() })
		pg, err := Open("postgres", dsn+"&search_path="+schema)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { pg.Close() })
		out["postgres"] = pg
	}
	return out
}

type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time      { c.mu.Lock(); defer c.mu.Unlock(); return c.t }
func (c *clock) add(d time.Duration) { c.mu.Lock(); c.t = c.t.Add(d); c.mu.Unlock() }

func withClock(s *Store) *clock {
	c := &clock{t: time.UnixMilli(1_767_225_600_000)}
	s.now = c.now
	return c
}

func TestQueuePriorityDebounceAndSingleFlight(t *testing.T) {
	for name, s := range stores(t) {
		t.Run(name, func(t *testing.T) {
			ctx := context.Background()
			c := withClock(s)
			review, _ := s.Enqueue(ctx, Task{Kind: KindReview, ProjectID: 1, MRIID: 7, Priority: 2,
				ThreadKey: "review:1:7"})
			c.add(time.Millisecond)
			reply, _ := s.Enqueue(ctx, Task{Kind: KindThread, ProjectID: 1, MRIID: 7, Priority: 0,
				ThreadKey: "thread:d1", Event: "reply", NotBefore: c.now().Add(time.Minute)})
			// The reply is debounced: the review goes first.
			got, err := s.Claim(ctx, "w1", time.Hour)
			if err != nil || got == nil || got.ID != review.ID {
				t.Fatalf("first claim = %+v, %v", got, err)
			}
			if none, _ := s.Claim(ctx, "w2", time.Hour); none != nil {
				t.Fatalf("claimed %s before its debounce", none.ID)
			}
			c.add(2 * time.Minute)
			// A push re-check on the same thread arrives while the reply is still queued:
			// one task answers, as a reply.
			push, _ := s.Enqueue(ctx, Task{Kind: KindThread, ProjectID: 1, MRIID: 7, Priority: 1,
				ThreadKey: "thread:d1", Event: "push"})
			if push.Event != "reply" || push.Priority != 0 {
				t.Fatalf("merged = %+v", push)
			}
			old, _ := s.Get(ctx, reply.ID)
			if old.Status != StatusSuperseded {
				t.Fatalf("reply status = %s", old.Status)
			}
			got, _ = s.Claim(ctx, "w2", time.Hour)
			if got == nil || got.ID != push.ID {
				t.Fatalf("second claim = %+v", got)
			}
			// While it runs, a newer event on the same thread waits (single-flight).
			next, _ := s.Enqueue(ctx, Task{Kind: KindThread, ProjectID: 1, MRIID: 7, Priority: 0,
				ThreadKey: "thread:d1", Event: "reply"})
			if blocked, _ := s.Claim(ctx, "w3", time.Hour); blocked != nil {
				t.Fatalf("claimed %s while its thread was running", blocked.ID)
			}
			if err := s.Finish(ctx, got.ID, StatusDone, ""); err != nil {
				t.Fatal(err)
			}
			got, _ = s.Claim(ctx, "w3", time.Hour)
			if got == nil || got.ID != next.ID {
				t.Fatalf("after finish = %+v", got)
			}
		})
	}
}

func TestQueueRecoversExpiredLeases(t *testing.T) {
	for name, s := range stores(t) {
		t.Run(name, func(t *testing.T) {
			ctx := context.Background()
			c := withClock(s)
			task, _ := s.Enqueue(ctx, Task{Kind: KindReview, ProjectID: 1, MRIID: 1, Priority: 2})
			for attempt := 1; attempt <= 3; attempt++ {
				got, _ := s.Claim(ctx, "w", time.Minute)
				if got == nil || got.Attempts != attempt {
					t.Fatalf("attempt %d: %+v", attempt, got)
				}
				c.add(2 * time.Minute) // the worker died
				if n, err := s.RecoverExpired(ctx, 3); err != nil || n != 1 {
					t.Fatalf("recover: %d %v", n, err)
				}
			}
			final, _ := s.Get(ctx, task.ID)
			if final.Status != StatusFailed {
				t.Fatalf("status = %s", final.Status)
			}
		})
	}
}

func TestQueueConcurrentClaimsNeverDoubleRun(t *testing.T) {
	for name, s := range stores(t) {
		t.Run(name, func(t *testing.T) {
			ctx := context.Background()
			for i := 0; i < 20; i++ {
				_, _ = s.Enqueue(ctx, Task{Kind: KindThread, ProjectID: 1, MRIID: 1, Priority: 1,
					ThreadKey: fmt.Sprintf("thread:%d", i%5)})
			}
			var mu sync.Mutex
			seen := map[string]bool{}
			var wg sync.WaitGroup
			for w := 0; w < 8; w++ {
				wg.Add(1)
				go func(w int) {
					defer wg.Done()
					got, err := s.Claim(ctx, fmt.Sprintf("w%d", w), time.Hour)
					if err != nil {
						t.Error(err)
						return
					}
					if got != nil {
						mu.Lock()
						if seen[got.ThreadKey] {
							t.Errorf("thread key %s claimed twice", got.ThreadKey)
						}
						seen[got.ThreadKey] = true
						mu.Unlock()
					}
				}(w)
			}
			wg.Wait()
		})
	}
}

func TestFindingStore(t *testing.T) {
	for name, s := range stores(t) {
		t.Run(name, func(t *testing.T) {
			ctx := context.Background()
			f := StoredFinding{ProjectID: 1, MRIID: 7, FindingID: "f1", DiscussionID: "d1", NoteID: 9,
				HeadSHA: "abc", FindingJSON: []byte(`{"id":"f1"}`),
				ChainPaths: []string{"store/users.py", "cache/users.py"}}
			if err := s.SaveFinding(ctx, f); err != nil {
				t.Fatal(err)
			}
			got, err := s.FindingByDiscussion(ctx, 1, 7, "d1")
			if err != nil || got == nil || got.ChainPaths[1] != "cache/users.py" || got.Status != "open" {
				t.Fatalf("got %+v %v", got, err)
			}
			if err := s.RecordVerdict(ctx, 1, 7, "f1", "withdrawn", "prefix middleware"); err != nil {
				t.Fatal(err)
			}
			open, _ := s.Findings(ctx, 1, 7, true)
			all, _ := s.Findings(ctx, 1, 7, false)
			if len(open) != 0 || len(all) != 1 || all[0].Lesson != "prefix middleware" {
				t.Fatalf("open %d all %+v", len(open), all)
			}
			if err := s.RecordVerdict(ctx, 1, 7, "f1", "stands", ""); err != nil {
				t.Fatal(err)
			}
			again, _ := s.FindingByDiscussion(ctx, 1, 7, "d1")
			if again.Status != "open" || again.Lesson != "prefix middleware" {
				t.Fatalf("again %+v", again)
			}
		})
	}
}
