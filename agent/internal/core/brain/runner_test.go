package brain

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"habodek/internal/pkg/llmproxy"
	"habodek/internal/pkg/sandbox"
)

// fakeBrain stands in for habodek-brain: it checks its environment, makes one LLM
// call through the proxy, and writes result.json.
const fakeBrain = `
import json, os, sys, urllib.request
args = sys.argv[1:]
kind, job_path = args[0], args[args.index("--job") + 1]
out = args[args.index("--out") + 1]
assert "GITLAB_TOKEN" not in os.environ, "GitLab token leaked to the brain"
assert "GW_KEY_SHOULD_NOT_LEAK" not in os.environ
job = json.load(open(job_path))["job"]
req = urllib.request.Request(os.environ["HABODEK_LLM_BASE_URL"] + "/chat/completions",
    data=json.dumps({"model": "qwen", "messages": []}).encode(),
    headers={"Authorization": "Bearer " + os.environ["HABODEK_LLM_TOKEN"],
             "Content-Type": "application/json"})
reply = json.load(urllib.request.urlopen(req))
result = {"contract": 1, "job_id": job["job_id"], "run_id": "r-1", "kind": kind,
          "status": "ok", "reason": "", "summary": reply["choices"][0]["message"]["content"],
          "comments": [{"finding_id": "f1", "path": "a.py", "line": 3, "side": "new",
                        "body": "b", "marker": "<!-- habodek:finding:f1 -->"}],
          "findings": [{"id": "f1", "title": "t", "claim": "c", "status": "posted",
                        "anchor": {"path": "a.py", "line": 3, "side": "new", "snippet": "x"},
                        "chain": [], "verifications": []}],
          "thread": None, "not_investigated": [], "stats": [], "requests": 1, "llm": {}}
json.dump(result, open(out, "w"))
`

func setup(t *testing.T, script string) (*Runner, *atomic.Int32, string) {
	t.Helper()
	var calls atomic.Int32
	gateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer real-gateway-key" {
			t.Errorf("gateway got auth %q", r.Header.Get("Authorization"))
		}
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"through the proxy"}}]}`))
	}))
	t.Cleanup(gateway.Close)
	proxy := llmproxy.New(llmproxy.Config{UpstreamURL: gateway.URL, GatewayKey: "real-gateway-key", RPM: 6000})
	mux := http.NewServeMux()
	mux.Handle(llmproxy.Path, proxy)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	dir := t.TempDir()
	scriptPath := filepath.Join(dir, "brain.py")
	if err := os.WriteFile(scriptPath, []byte(script), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GITLAB_TOKEN", "secret")
	t.Setenv("GW_KEY_SHOULD_NOT_LEAK", "1")
	runner := NewRunner(RunnerConfig{Command: "python3", Args: []string{scriptPath},
		WorkDir: filepath.Join(dir, "work"), TraceDir: filepath.Join(dir, "traces"),
		ProxyURL: server.URL + "/internal/llm/v1", Timeout: time.Minute}, proxy)
	return runner, &calls, dir
}

func sampleJob() Job {
	budget := 300
	return Job{Kind: "review", JobID: "job-42", Priority: "review", RequestBudget: &budget,
		MR: MergeRequest{Project: "team/svc", IID: 7, Title: "Change", Labels: []string{"ai-review"},
			WebURL: "https://gitlab.example/team/svc/-/merge_requests/7"},
		Revisions: Revisions{BaseSHA: strings.Repeat("a", 40), HeadSHA: strings.Repeat("b", 40)},
		Workspace: Workspace{Head: "/w/head", Base: "/w/base", Repo: "/w/repo",
			OtherRepos: map[string]string{"orders-service": "/w/orders"}},
		MaxComments: 6}
}

func TestRunnerRunsTheBrainThroughTheProxy(t *testing.T) {
	runner, calls, _ := setup(t, fakeBrain)
	result, err := runner.Run(context.Background(), sampleJob())
	if err != nil {
		t.Fatal(err)
	}
	if result.Summary != "through the proxy" || calls.Load() != 1 {
		t.Fatalf("summary %q, gateway calls %d", result.Summary, calls.Load())
	}
	if len(result.Posted()) != 1 || result.Comments[0].Line != 3 {
		t.Fatalf("result = %+v", result)
	}
	trace, err := os.ReadFile(filepath.Join(runner.TraceDir("job-42"), "llm-proxy.jsonl"))
	if err != nil || !strings.Contains(string(trace), `"job":"job-42"`) {
		t.Fatalf("proxy trace missing: %v", err)
	}
	if strings.Contains(string(trace), "real-gateway-key") {
		t.Fatal("gateway key in the trace")
	}
}

func TestRunnerReportsAFailingBrain(t *testing.T) {
	runner, _, _ := setup(t, "import sys\nsys.stderr.write('boom: no model')\nsys.exit(3)\n")
	_, err := runner.Run(context.Background(), sampleJob())
	if err == nil || !strings.Contains(err.Error(), "boom: no model") {
		t.Fatalf("err = %v", err)
	}
}

func TestRunnerTokenIsRevokedAfterTheJob(t *testing.T) {
	runner, _, _ := setup(t, fakeBrain)
	if _, err := runner.Run(context.Background(), sampleJob()); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(filepath.Join(runner.JobDir("job-42"), "job.json"))
	if strings.Contains(string(data), "hbk_") {
		t.Fatal("token written into job.json")
	}
}

// TestGoldenJob writes testdata/job_review.json and testdata/job_thread.json; the Python
// test suite validates both against the pydantic contract (tests/test_go_contract.py).
func TestGoldenJob(t *testing.T) {
	review := sampleJob()
	review.Contract = ContractVersion
	review.Workspace.Dependencies = []DependencyTree{{Name: "payments-lib", Version: "1.5.0",
		Path: "/deps/payments-lib/1.5.0", Side: "head"}}
	review.Policy = &Policy{Bars: []CategoryBar{{Category: "consumers", MinSeverity: "critical",
		Acceptance: 0.2, Outcomes: 10}}}
	thread := sampleJob()
	thread.Contract = ContractVersion
	thread.Kind, thread.JobID, thread.Priority, thread.Event = "thread", "job-43", "reply", "reply"
	thread.MaxComments = 0
	dropped := "verify_a"
	thread.Finding = &Finding{ID: "f1", Title: "t", Claim: "c", Severity: "major",
		Anchor:  Anchor{Path: "a.py", Line: 3, Side: "new", Snippet: "x"},
		Chain:   []EvidenceLink{{Path: "a.py", Line: 3, Quote: "x", Claim: "y", Repo: "repo:orders-service"}},
		HeadSHA: strings.Repeat("b", 40), Status: "posted", DroppedAt: nil}
	_ = dropped
	thread.Notes = []Note{{Author: "dev", Body: "fixed"}, {Author: "habodek", Body: "…", IsBot: true}}
	mem, commit := "/var/lib/habodek/memory", strings.Repeat("c", 40)
	mapJob := Job{Contract: ContractVersion, Kind: "map", JobID: "job-44", Priority: "background",
		MR:        MergeRequest{Project: "team/svc", ProjectID: 12, TargetBranch: "main"},
		Revisions: Revisions{BaseSHA: strings.Repeat("d", 40), HeadSHA: strings.Repeat("d", 40)},
		Workspace: Workspace{Head: "/w/head", Repo: "/w/repo"}, MemoryPath: &mem, MemoryCommit: &commit}
	for name, job := range map[string]Job{"job_review.json": review, "job_thread.json": thread,
		"job_map.json": mapJob} {
		data, err := json.MarshalIndent(Envelope{Job: job}, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join("testdata", name)
		if os.Getenv("UPDATE_GOLDEN") != "" {
			if err := os.WriteFile(path, append(data, '\n'), 0o644); err != nil {
				t.Fatal(err)
			}
			continue
		}
		want, err := os.ReadFile(path)
		if err != nil || strings.TrimSpace(string(want)) != string(data) {
			t.Fatalf("%s is stale; regenerate with UPDATE_GOLDEN=1 go test ./internal/core/brain/", name)
		}
	}
}

// TestParsesARealBrainResult reads a result.json produced by the Python brain.
func TestParsesARealBrainResult(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("testdata", "result_review.json"))
	if err != nil {
		t.Fatal(err)
	}
	var result Result
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields() // a field the Go side does not know is a contract drift
	if err := dec.Decode(&result); err != nil {
		t.Fatal(err)
	}
	if result.Contract != ContractVersion || len(result.Comments) != 1 || len(result.Posted()) != 1 {
		t.Fatalf("result = %+v", result)
	}
	if !strings.Contains(result.Comments[0].Body, result.Comments[0].Marker) {
		t.Fatal("comment body lacks its marker")
	}
}

type recordingSandbox struct {
	registered map[string]sandbox.Job
	revoked    []string
}

func (r *recordingSandbox) Register(token string, job sandbox.Job) {
	if r.registered == nil {
		r.registered = map[string]sandbox.Job{}
	}
	r.registered[token] = job
}
func (r *recordingSandbox) Unregister(token string) { r.revoked = append(r.revoked, token) }

// The brain gets the sandbox URL, and its job token may run exactly the job's head,
// base and other repositories — never dependency sources — until the job ends.
func TestRunnerGivesTheJobItsSandboxRoots(t *testing.T) {
	script := strings.Replace(fakeBrain, "reply = json.load",
		`assert os.environ["HABODEK_SANDBOX_URL"] == "http://cp/internal/sandbox/v1"
reply = json.load`, 1)
	runner, _, _ := setup(t, script)
	runner.cfg.SandboxURL = "http://cp/internal/sandbox/v1"
	sb := &recordingSandbox{}
	runner.WithSandbox(sb)
	job := sampleJob()
	job.Workspace.Dependencies = []DependencyTree{{Name: "payments-lib", Version: "1.5.0", Path: "/w/dep"}}
	if _, err := runner.Run(context.Background(), job); err != nil {
		t.Fatal(err)
	}
	if len(sb.registered) != 1 || len(sb.revoked) != 1 {
		t.Fatalf("registered %v revoked %v", sb.registered, sb.revoked)
	}
	for token, got := range sb.registered {
		want := map[string]string{"head": "/w/head", "base": "/w/base", "repo:orders-service": "/w/orders"}
		if !reflect.DeepEqual(got.Roots, want) || got.ID != "job-42" || sb.revoked[0] != token {
			t.Fatalf("job %+v", got)
		}
	}
}
