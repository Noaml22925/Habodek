// The Habodek control plane: GitLab webhooks in, reviews and thread answers out.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/joho/godotenv"

	"habodek/internal/app"
	"habodek/internal/config"
	"habodek/internal/pkg/llmproxy"
	"habodek/internal/pkg/logger"
)

func main() {
	_ = godotenv.Load()
	cfg, err := config.Load()
	if err != nil {
		slog.Error("invalid configuration", "error", err)
		os.Exit(1)
	}
	logger.Setup(cfg.Log.Level, cfg.Log.Format)
	if err := cfg.ValidateForServer(); err != nil {
		slog.Error("invalid server configuration", "error", err)
		os.Exit(1)
	}
	a, err := app.Build(cfg)
	if err != nil {
		slog.Error("startup", "error", err)
		os.Exit(1)
	}
	defer a.Close()

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	workers := a.Worker.Start(ctx)

	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(middleware.RequestID)
	r.Post("/webhook/gitlab", a.Triggers.ServeHTTP)
	r.Get("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	// The brain's only route to the models: per-job tokens, per-model limits, raw traces.
	r.Post(llmproxy.Path, a.Proxy.ServeHTTP)

	addr := fmt.Sprintf("%s:%s", cfg.Server.Host, cfg.Server.Port)
	server := &http.Server{Addr: addr, Handler: r, ReadHeaderTimeout: 30 * time.Second}
	slog.Info("habodek control plane listening", "addr", addr, "store", cfg.Store.Driver,
		"workers", cfg.Worker.PoolSize, "gateway_rpm_per_model", cfg.Brain.GatewayRPM)
	go func() {
		if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("server error", "error", err)
			os.Exit(1)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	slog.Info("shutting down; running tasks are re-queued when their lease expires")
	shutdown, done := context.WithTimeout(context.Background(), 30*time.Second)
	defer done()
	_ = server.Shutdown(shutdown)
	cancel()
	stopped := make(chan struct{})
	go func() { workers.Wait(); close(stopped) }()
	select {
	case <-stopped:
	case <-shutdown.Done():
		slog.Warn("shutdown timeout; some brain jobs were interrupted")
	}
}
