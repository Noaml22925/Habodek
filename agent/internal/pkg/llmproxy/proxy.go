package llmproxy

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Path is where the proxy is mounted on the control plane's server.
const Path = "/internal/llm/v1/chat/completions"

const maxBody = 64 << 20

// Job is what a per-job token stands for.
type Job struct {
	ID       string
	Priority int
	TraceDir string // raw traffic goes to TraceDir/llm-proxy.jsonl ("" = not logged)
}

// Config of the proxy.
type Config struct {
	UpstreamURL string // the LiteLLM base URL, e.g. https://litellm.internal/v1
	GatewayKey  string
	RPM         float64 // per model
	Burst       float64
	Timeout     time.Duration
	// MaxAttempts bounds retries on upstream 429 inside the proxy; the brain retries too.
	MaxAttempts int
}

// Proxy implements http.Handler for Path.
type Proxy struct {
	cfg    Config
	pool   *Pool
	client *http.Client
	jobs   sync.Map // token -> Job
	logMu  sync.Mutex
}

func New(cfg Config) *Proxy {
	if cfg.RPM <= 0 {
		cfg.RPM = 25
	}
	if cfg.Burst <= 0 {
		cfg.Burst = 2
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 15 * time.Minute
	}
	if cfg.MaxAttempts <= 0 {
		cfg.MaxAttempts = 3
	}
	return &Proxy{cfg: cfg, pool: NewPool(cfg.RPM, cfg.Burst),
		client: &http.Client{Timeout: cfg.Timeout}}
}

// Pool exposes the per-model limiters (for stats).
func (p *Proxy) Pool() *Pool { return p.pool }

// Register issues a token for a job; the caller passes it to the brain subprocess.
func (p *Proxy) Register(job Job) string {
	buf := make([]byte, 24)
	if _, err := rand.Read(buf); err != nil {
		panic(err)
	}
	token := "hbk_" + hex.EncodeToString(buf)
	p.jobs.Store(token, job)
	return token
}

// Unregister revokes a token when the job ends.
func (p *Proxy) Unregister(token string) { p.jobs.Delete(token) }

func (p *Proxy) upstream() string {
	base := strings.TrimRight(p.cfg.UpstreamURL, "/")
	if strings.HasSuffix(base, "/chat/completions") {
		return base
	}
	if strings.HasSuffix(base, "/v1") {
		return base + "/chat/completions"
	}
	return base + "/v1/chat/completions"
}

func writeError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"message": msg}})
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "POST only")
		return
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	value, ok := p.jobs.Load(token)
	if !ok || token == "" {
		writeError(w, http.StatusUnauthorized, "unknown job token")
		return
	}
	job := value.(Job)
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBody+1))
	if err != nil || len(body) > maxBody {
		writeError(w, http.StatusRequestEntityTooLarge, "request body too large or unreadable")
		return
	}
	var head struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(body, &head); err != nil || head.Model == "" {
		writeError(w, http.StatusBadRequest, "body must be JSON with a model")
		return
	}
	limiter := p.pool.For(head.Model)
	var resp *http.Response
	var respBody []byte
	var waited time.Duration
	started := time.Now()
	for attempt := 1; ; attempt++ {
		wait, err := limiter.Acquire(r.Context(), job.Priority)
		waited += wait
		if err != nil {
			writeError(w, 499, "client went away while queued")
			return
		}
		resp, respBody, err = p.forward(r, body)
		if err != nil {
			p.log(job, head.Model, body, 0, nil, []byte(err.Error()), waited, time.Since(started))
			writeError(w, http.StatusBadGateway, "gateway unreachable: "+err.Error())
			return
		}
		if resp.StatusCode == http.StatusTooManyRequests {
			pause := limiter.OnThrottle(parseRetryAfter(resp.Header.Get("Retry-After")))
			p.log(job, head.Model, body, resp.StatusCode, resp.Header, respBody, waited,
				time.Since(started))
			if attempt < p.cfg.MaxAttempts {
				continue // the limiter now holds everyone back for `pause`
			}
			resp.Header.Set("Retry-After", strconv.Itoa(int(pause.Seconds()+0.5)))
			break
		}
		if resp.StatusCode < 400 {
			limiter.OnSuccess()
		}
		p.log(job, head.Model, body, resp.StatusCode, resp.Header, respBody, waited,
			time.Since(started))
		break
	}
	for key, values := range resp.Header {
		lower := strings.ToLower(key)
		if lower == "content-type" || lower == "retry-after" || strings.HasPrefix(lower, "x-ratelimit") {
			for _, v := range values {
				w.Header().Add(key, v)
			}
		}
	}
	w.Header().Set("X-Habodek-Waited-Ms", strconv.FormatInt(waited.Milliseconds(), 10))
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(respBody)
}

func (p *Proxy) forward(r *http.Request, body []byte) (*http.Response, []byte, error) {
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, p.upstream(),
		bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if p.cfg.GatewayKey != "" {
		req.Header.Set("Authorization", "Bearer "+p.cfg.GatewayKey)
	}
	resp, err := p.client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, nil, err
	}
	return resp, data, nil
}

func parseRetryAfter(value string) time.Duration {
	if value == "" {
		return 0
	}
	if secs, err := strconv.ParseFloat(strings.TrimSpace(value), 64); err == nil && secs >= 0 {
		return time.Duration(secs * float64(time.Second))
	}
	if t, err := http.ParseTime(value); err == nil {
		return time.Until(t)
	}
	return 0
}

// log appends one record of raw traffic to the job's trace. The gateway key is never
// in it: the brain's request carried only its job token, which is not recorded.
func (p *Proxy) log(job Job, model string, req []byte, status int, headers http.Header,
	resp []byte, waited, elapsed time.Duration) {
	if job.TraceDir == "" {
		return
	}
	record := map[string]any{
		"ts": time.Now().UTC().Format(time.RFC3339Nano), "job": job.ID, "model": model,
		"priority": job.Priority, "status": status, "waited_ms": waited.Milliseconds(),
		"elapsed_ms": elapsed.Milliseconds(),
	}
	record["request"] = rawOrString(req)
	record["response"] = rawOrString(resp)
	if headers != nil {
		kept := map[string]string{}
		for key := range headers {
			lower := strings.ToLower(key)
			if lower == "retry-after" || strings.HasPrefix(lower, "x-ratelimit") ||
				strings.HasPrefix(lower, "x-litellm") {
				kept[lower] = headers.Get(key)
			}
		}
		record["headers"] = kept
	}
	line, err := json.Marshal(record)
	if err != nil {
		return
	}
	p.logMu.Lock()
	defer p.logMu.Unlock()
	if err := os.MkdirAll(job.TraceDir, 0o755); err != nil {
		slog.Warn("llm proxy: cannot create trace dir", "dir", job.TraceDir, "error", err)
		return
	}
	f, err := os.OpenFile(filepath.Join(job.TraceDir, "llm-proxy.jsonl"),
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		slog.Warn("llm proxy: cannot write trace", "error", err)
		return
	}
	defer f.Close()
	_, _ = fmt.Fprintf(f, "%s\n", line)
}

func rawOrString(data []byte) any {
	if json.Valid(data) {
		return json.RawMessage(data)
	}
	return string(data)
}
