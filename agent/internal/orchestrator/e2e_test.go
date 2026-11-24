package orchestrator

import (
	"bufio"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"habodek/internal/core/brain"
	"habodek/internal/pkg/git"
	"habodek/internal/pkg/gitlab"
	"habodek/internal/pkg/llmproxy"
	"habodek/internal/pkg/memory"
	"habodek/internal/pkg/sandbox"
	"habodek/internal/testutil/fakegitlab"
)

// TestEndToEnd runs the whole v2 path with the real Python brain (handoff M8 "done
// when"): label -> in-progress note -> a thread with its evidence chain -> a reply ->
// a verdict in the thread. GitLab is faked, the gateway serves the scripted model
// (tests/e2e_gateway.py) and the repository is scenario S2's orders-service.
//
// Where unprivileged namespaces work, the brain also gets the sandbox (M9 "done
// when"): the investigator runs the repository's tests through this control plane's
// sandbox endpoint, they fail at head and pass at base, and the posted comment cites
// the failing run.
//
// Memory (M10): the first review of the project enqueues the cartographer, which runs
// through the real brain and commits a repo card to the memory repository; the reply
// job that follows reads memory at that commit.
//
// Needs python3 with the habodek package installed and habodek-brain on PATH; run
// with HABODEK_E2E=1 (CI and the scorecard machine do).
func TestEndToEnd(t *testing.T) {
	if os.Getenv("HABODEK_E2E") == "" {
		t.Skip("set HABODEK_E2E=1 to run the end-to-end test with the real brain")
	}
	root, _ := filepath.Abs("../../..")
	work := t.TempDir()
	built := filepath.Join(work, "scenarios")

	// 1. The gateway: the scripted model behind the real chat templates.
	gw := exec.Command("python3", filepath.Join(root, "tests", "e2e_gateway.py"), "--built", built)
	gw.Dir = root
	stdout, _ := gw.StdoutPipe()
	gw.Stderr = os.Stderr
	if err := gw.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = gw.Process.Kill(); _ = gw.Wait() })
	line, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil {
		t.Fatalf("gateway did not start: %v", err)
	}
	gatewayURL := strings.TrimSpace(line)

	// 2. The mirror: scenario S2's orders-service, as if fetched from GitLab.
	reposDir := filepath.Join(work, "repos")
	mirror := filepath.Join(reposDir, "1")
	runGit(t, "", "clone", "-q", filepath.Join(built, "repos", "orders-service"), mirror)
	runGit(t, mirror, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
	head := runGit(t, mirror, "rev-parse", "origin/mr/s2-payments-1.5.0")
	base := runGit(t, mirror, "rev-parse", "origin/main")

	// 3. GitLab, the store, the proxy, the runner, the worker: the real ones.
	gl := fakegitlab.New(botID)
	t.Cleanup(gl.Close)
	gl.MRs[2] = &fakegitlab.MR{IID: 2, Title: "Bump payments-lib to 1.5.0",
		Description: "Routine dependency update.", TargetBranch: "main", SourceBranch: "mr/s2-payments-1.5.0",
		HeadSHA: head, BaseSHA: base, StartSHA: base, Labels: []string{"ai-review"}, Author: "dev-b"}
	store, err := Open("sqlite3", filepath.Join(work, "hbk.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	proxy := llmproxy.New(llmproxy.Config{UpstreamURL: gatewayURL, GatewayKey: "gw-key", RPM: 6000})
	mux := http.NewServeMux()
	mux.Handle(llmproxy.Path, proxy)
	var sb *sandbox.Service
	if sandbox.LocalAvailable() {
		sb, err = sandbox.New(sandbox.Config{CacheDir: filepath.Join(work, "sandbox"),
			WorkDir: filepath.Join(work, "sandbox-runs"), FindLinks: []string{filepath.Join(built, "index")}},
			&sandbox.Local{})
		if err != nil {
			t.Fatal(err)
		}
		mux.Handle(sandbox.Path, sb)
	} else {
		t.Log("no user namespaces: running without the sandbox")
	}
	proxySrv := httptest.NewServer(mux)
	t.Cleanup(proxySrv.Close)
	traces := filepath.Join(work, "traces")
	runner := brain.NewRunner(brain.RunnerConfig{Command: "habodek-brain", WorkDir: filepath.Join(work, "jobs"),
		TraceDir: traces, ProxyURL: proxySrv.URL + "/internal/llm/v1", Timeout: 5 * time.Minute,
		InternalPackages: "payments-lib,platform-common",
		SandboxURL:       proxySrv.URL + "/internal/sandbox/v1",
		ExtraEnv:         []string{"HABODEK_PIP_FIND_LINKS=" + filepath.Join(built, "index")}}, proxy)
	if sb != nil {
		runner.WithSandbox(sb)
	}
	gm := &localGit{Manager: git.NewManager(reposDir, gl.URL(), "")}
	client := gitlab.NewClient(gl.URL(), "token")
	worker := &Worker{Store: store, GitLab: client, Poster: &Poster{GitLab: client}, Brain: runner,
		Workspaces: &brain.Workspaces{Git: gm, WorkDir: filepath.Join(work, "jobs")}, Git: gm,
		Config: WorkerConfig{BotUserID: botID, MaxComments: 6, Lease: 10 * time.Minute},
		Memory: memory.New(filepath.Join(work, "memory")), Applier: runner}
	r := &rig{store: store, gl: gl, worker: worker,
		triggers: &Triggers{Store: store, WebhookSecret: "s3cret", BotUserID: botID, TriggerLabel: "ai-review",
			Debounce: 100 * time.Millisecond}}
	r.hook = httptest.NewServer(r.triggers)
	t.Cleanup(r.hook.Close)

	// 4. Label -> review.
	ev := labelEvent()
	ev["object_attributes"].(map[string]any)["iid"] = 2
	r.webhook(t, "merge_request", ev)
	r.drainSlow(t, 4*time.Minute)
	all := strings.Join(bodies(gl.Snapshot(2)), "\n----\n")
	for _, want := range []string{"Habodek is reviewing", "payments-lib 1.5.0 returns declined charges",
		"`orders/domain/checkout.py:20`", "payments-lib@1.5.0 `payments/client.py:32`", "<!-- habodek:summary:" + head} {
		if !strings.Contains(all, want) {
			t.Fatalf("missing %q in GitLab notes:\n%s", want, all)
		}
	}
	findings, _ := store.Findings(context.Background(), 1, 2, true)
	if len(findings) != 1 || findings[0].HeadSHA != head {
		t.Fatalf("findings = %+v", findings)
	}
	var inline *fakegitlab.Note
	for _, d := range gl.Snapshot(2) {
		if d.ID == findings[0].DiscussionID {
			inline = d.Notes[0]
		}
	}
	if inline == nil || inline.Position["new_path"] != "requirements.txt" || inline.Position["new_line"].(float64) != 2 {
		t.Fatalf("anchor = %+v", inline)
	}
	if sb != nil {
		for _, want := range []string{"Reproduced in the sandbox", "on this MR's head fails (exit 1)",
			"on the target branch (before the change) passes", "test_declined_cancels"} {
			if !strings.Contains(inline.Body, want) {
				t.Fatalf("missing %q in the comment:\n%s", want, inline.Body)
			}
		}
		runs, _ := filepath.Glob(filepath.Join(traces, "*", "sandbox.jsonl"))
		var log string
		for _, m := range runs {
			data, _ := os.ReadFile(m)
			log += string(data)
		}
		if !strings.Contains(log, `"root":"head"`) || !strings.Contains(log, `"exit_code":1`) ||
			!strings.Contains(log, `"root":"base"`) || !strings.Contains(log, `"exit_code":0`) {
			t.Fatalf("sandbox log:\n%s", log)
		}
	}

	// 4b. The first review mapped the project: a verified card is in memory.
	card, err := os.ReadFile(filepath.Join(work, "memory", "projects", "group", "project", "card.md"))
	if err != nil {
		t.Fatalf("no repo card after the first review: %v", err)
	}
	for _, want := range []string{"owns_refund middleware", "`orders/app.py:", "## Team notes"} {
		if !strings.Contains(string(card), want) {
			t.Fatalf("card lacks %q:\n%s", want, card)
		}
	}
	memCommit := worker.Memory.Head(context.Background())
	if facts, ok := worker.Memory.Facts("group/project"); !ok || facts.GitLabID != 1 || facts.Package != "orders-service" {
		t.Fatalf("facts %+v", facts)
	}

	// 5. A reply -> the thread agent -> a verdict in the thread.
	gl.HumanReply(2, findings[0].DiscussionID, 5, "We only bumped a patch-level dependency, this is fine.")
	note := notePayload(5, findings[0].DiscussionID)
	note["merge_request"] = map[string]any{"iid": 2}
	r.webhook(t, "note", note)
	r.drainSlow(t, 4*time.Minute)
	var reply string
	for _, d := range gl.Snapshot(2) {
		if d.ID == findings[0].DiscussionID {
			reply = d.Notes[len(d.Notes)-1].Body
		}
	}
	if !strings.Contains(reply, "This still applies") || !strings.Contains(reply, "habodek:reply:") {
		t.Fatalf("reply = %q", reply)
	}
	after, _ := store.FindingByDiscussion(context.Background(), 1, 2, findings[0].DiscussionID)
	if after.Verdict != "stands" || gl.Resolved != 0 {
		t.Fatalf("verdict %q resolved %d", after.Verdict, gl.Resolved)
	}
	results, _ := filepath.Glob(filepath.Join(traces, "*", "result.json"))
	var sawThread bool
	for _, m := range results {
		var res brain.Result
		data, _ := os.ReadFile(m)
		if json.Unmarshal(data, &res) == nil && res.Kind == "thread" {
			sawThread = true
			if res.MemoryCommit != memCommit {
				t.Fatalf("thread read memory %q, want %q", res.MemoryCommit, memCommit)
			}
		}
	}
	if !sawThread {
		t.Fatal("no thread result")
	}
	// The proxy recorded the raw traffic of both jobs, without the gateway key.
	matches, _ := filepath.Glob(filepath.Join(traces, "*", "llm-proxy.jsonl"))
	if len(matches) != 3 { // review, map, reply
		t.Fatalf("proxy traces = %v", matches)
	}
	for _, m := range matches {
		data, _ := os.ReadFile(m)
		if strings.Contains(string(data), "gw-key") {
			t.Fatal("gateway key in a trace")
		}
	}
}

func (r *rig) drainSlow(t *testing.T, limit time.Duration) {
	t.Helper()
	deadline := time.Now().Add(limit)
	for time.Now().Before(deadline) {
		task, err := r.store.Claim(context.Background(), "e2e", 10*time.Minute)
		if err != nil {
			t.Fatal(err)
		}
		if task == nil {
			var queued int
			_ = r.store.db.QueryRow(`SELECT COUNT(*) FROM hbk_tasks WHERE status = 'queued'`).Scan(&queued)
			if queued == 0 {
				return
			}
			time.Sleep(50 * time.Millisecond)
			continue
		}
		r.worker.RunTask(context.Background(), task)
		got, _ := r.store.Get(context.Background(), task.ID)
		if got.Status != StatusDone {
			t.Fatalf("task %s (%s) %s: %s", task.ID, task.Kind, got.Status, got.Error)
		}
	}
	t.Fatal("queue did not drain")
}

type localGit struct{ *git.Manager }

func (l *localGit) FetchAndCheckout(context.Context, int64, string, int64, string, string) error {
	return nil
}

func runGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %s", args, out)
	}
	return strings.TrimSpace(string(out))
}
