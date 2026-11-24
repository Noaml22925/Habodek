package llmproxy

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestLimiterServesHigherPriorityFirst(t *testing.T) {
	l := NewLimiter(1200, 1) // one token per 50 ms
	if _, err := l.Acquire(context.Background(), PriorityReview); err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var order []int
	var wg sync.WaitGroup
	for _, prio := range []int{PriorityBackground, PriorityReview, PriorityRecheck, PriorityReply} {
		wg.Add(1)
		go func(p int) {
			defer wg.Done()
			if _, err := l.Acquire(context.Background(), p); err != nil {
				t.Error(err)
			}
			mu.Lock()
			order = append(order, p)
			mu.Unlock()
		}(prio)
		time.Sleep(5 * time.Millisecond)
	}
	wg.Wait()
	want := []int{PriorityReply, PriorityRecheck, PriorityReview, PriorityBackground}
	for i := range want {
		if order[i] != want[i] {
			t.Fatalf("order = %v, want %v", order, want)
		}
	}
}

func TestLimiterThrottleHalvesAndPauses(t *testing.T) {
	l := NewLimiter(1200, 1)
	l.OnThrottle(150 * time.Millisecond)
	start := time.Now()
	if _, err := l.Acquire(context.Background(), PriorityReply); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) < 140*time.Millisecond {
		t.Fatalf("did not pause: %v", time.Since(start))
	}
	if got := l.Stats().RPM; got != 600 {
		t.Fatalf("rpm = %v, want 600", got)
	}
}

func TestLimiterCancelLeavesQueueClean(t *testing.T) {
	l := NewLimiter(1, 1)
	_, _ = l.Acquire(context.Background(), PriorityReview)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if _, err := l.Acquire(ctx, PriorityReview); err == nil {
		t.Fatal("expected a timeout")
	}
	if l.Stats().Waiting != 0 {
		t.Fatal("cancelled waiter still queued")
	}
}

type upstream struct {
	srv      *httptest.Server
	calls    atomic.Int32
	throttle map[string]int // model -> number of 429s to answer first
	mu       sync.Mutex
	auth     []string
}

func newUpstream(t *testing.T) *upstream {
	u := &upstream{throttle: map[string]int{}}
	u.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u.calls.Add(1)
		body, _ := io.ReadAll(r.Body)
		var req struct{ Model string }
		_ = json.Unmarshal(body, &req)
		u.mu.Lock()
		u.auth = append(u.auth, r.Header.Get("Authorization"))
		left := u.throttle[req.Model]
		if left > 0 {
			u.throttle[req.Model] = left - 1
		}
		u.mu.Unlock()
		if left > 0 {
			w.Header().Set("Retry-After", "0.05")
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":{"message":"rate limited"}}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("X-Ratelimit-Remaining-Requests", "24")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],"model":"` + req.Model + `"}`))
	}))
	t.Cleanup(u.srv.Close)
	return u
}

func post(t *testing.T, handler http.Handler, token, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, Path, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+token)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return rec
}

func TestProxyForwardsWithGatewayKeyAndLogs(t *testing.T) {
	up := newUpstream(t)
	p := New(Config{UpstreamURL: up.srv.URL + "/v1", GatewayKey: "gw-secret", RPM: 6000})
	dir := t.TempDir()
	token := p.Register(Job{ID: "job-1", Priority: PriorityReply, TraceDir: dir})
	rec := post(t, p, token, `{"model":"qwen","messages":[{"role":"user","content":"hi"}]}`)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"content":"ok"`) {
		t.Fatalf("status %d body %s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get("X-Ratelimit-Remaining-Requests") != "24" {
		t.Fatal("rate-limit header not passed through")
	}
	if up.auth[0] != "Bearer gw-secret" {
		t.Fatalf("upstream auth = %q", up.auth[0])
	}
	data, err := os.ReadFile(filepath.Join(dir, "llm-proxy.jsonl"))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "gw-secret") || strings.Contains(string(data), token) {
		t.Fatal("trace contains a secret")
	}
	var rec1 map[string]any
	if err := json.NewDecoder(bufio.NewReader(strings.NewReader(string(data)))).Decode(&rec1); err != nil {
		t.Fatal(err)
	}
	if rec1["job"] != "job-1" || rec1["status"].(float64) != 200 || rec1["model"] != "qwen" {
		t.Fatalf("record = %v", rec1)
	}
	if rec1["request"].(map[string]any)["model"] != "qwen" {
		t.Fatal("raw request not recorded as JSON")
	}
}

func TestProxyRejectsUnknownTokensAndBadBodies(t *testing.T) {
	up := newUpstream(t)
	p := New(Config{UpstreamURL: up.srv.URL, RPM: 6000})
	if rec := post(t, p, "nope", `{"model":"m"}`); rec.Code != http.StatusUnauthorized {
		t.Fatalf("unknown token: %d", rec.Code)
	}
	token := p.Register(Job{ID: "j"})
	if rec := post(t, p, token, `not json`); rec.Code != http.StatusBadRequest {
		t.Fatalf("bad body: %d", rec.Code)
	}
	p.Unregister(token)
	if rec := post(t, p, token, `{"model":"m"}`); rec.Code != http.StatusUnauthorized {
		t.Fatalf("revoked token: %d", rec.Code)
	}
	if up.calls.Load() != 0 {
		t.Fatal("rejected requests reached the gateway")
	}
}

func TestProxyThrottlesOnlyTheModelThat429d(t *testing.T) {
	up := newUpstream(t)
	up.throttle["qwen"] = 1
	p := New(Config{UpstreamURL: up.srv.URL, RPM: 6000, Burst: 5})
	token := p.Register(Job{ID: "j", Priority: PriorityReview})
	if rec := post(t, p, token, `{"model":"qwen"}`); rec.Code != 200 {
		t.Fatalf("retry after 429 inside the proxy failed: %d", rec.Code)
	}
	if rec := post(t, p, token, `{"model":"glm"}`); rec.Code != 200 {
		t.Fatalf("glm: %d", rec.Code)
	}
	if p.Pool().For("qwen").Stats().Throttles != 1 || p.Pool().For("glm").Stats().Throttles != 0 {
		t.Fatal("a 429 on qwen must not throttle glm")
	}
	if p.Pool().For("qwen").Stats().RPM != 3000 {
		t.Fatalf("qwen rpm = %v", p.Pool().For("qwen").Stats().RPM)
	}
}

func TestProxyReturns429AfterMaxAttempts(t *testing.T) {
	up := newUpstream(t)
	up.throttle["qwen"] = 10
	p := New(Config{UpstreamURL: up.srv.URL, RPM: 6000, MaxAttempts: 2})
	token := p.Register(Job{ID: "j"})
	rec := post(t, p, token, `{"model":"qwen"}`)
	if rec.Code != http.StatusTooManyRequests || rec.Header().Get("Retry-After") == "" {
		t.Fatalf("status %d retry-after %q", rec.Code, rec.Header().Get("Retry-After"))
	}
	if up.calls.Load() != 2 {
		t.Fatalf("upstream calls = %d", up.calls.Load())
	}
}
