package di

import (
	"github.com/samber/do/v2"

	"habodek/internal/config"
	"habodek/internal/core/feedback"
	"habodek/internal/core/reply"
	"habodek/internal/core/review"
	"habodek/internal/domain"
	"habodek/internal/handler/cron"
	"habodek/internal/handler/webhook"
	"habodek/internal/handler/worker"
	"habodek/internal/pkg/queue"
)

var HandlerPackage = do.Package(
	do.Lazy(provideWorkerPool),
	do.Lazy(provideCronRunner),
)

func provideWorkerPool(i do.Injector) (*worker.Pool, error) {
	cfg := do.MustInvoke[*config.Config](i)
	return worker.NewPool(cfg.Worker.PoolSize, worker.PoolDeps{
		Queue:          do.MustInvoke[*queue.Queue](i),
		ReviewPipeline: do.MustInvoke[*review.Pipeline](i),
		ReplyPipeline:  do.MustInvoke[*reply.Pipeline](i),
		ReviewJobStore: do.MustInvoke[domain.ReviewJobStore](i),
		ReplyJobStore:  do.MustInvoke[domain.ReplyJobStore](i),
	}), nil
}

func provideCronRunner(i do.Injector) (*cron.Runner, error) {
	cfg := do.MustInvoke[*config.Config](i)
	r := cron.NewRunner()

	consolidator, err := do.Invoke[*feedback.Consolidator](i)
	if err == nil {
		job := cron.NewFeedbackConsolidatorJob(
			do.MustInvoke[domain.RepositorySettingsStore](i),
			consolidator,
		)
		r.Register(cfg.Cron.FeedbackConsolidateSchedule, "feedback_consolidator", job.Run)
	}

	staleRecovery := cron.NewStaleJobRecoveryJob(
		do.MustInvoke[domain.ReviewJobStore](i),
		35,
	)
	r.Register("*/5 * * * *", "stale_job_recovery", staleRecovery.Run)

	return r, nil
}

// ProvideWebhookHandler creates the webhook handler with the given server context.
func ProvideWebhookHandler(i do.Injector) (*webhook.Handler, error) {
	cfg := do.MustInvoke[*config.Config](i)
	if err := cfg.ValidateForServer(); err != nil {
		return nil, err
	}
	return webhook.NewHandler(webhook.HandlerDeps{
		WebhookSecret:      cfg.GitLab.WebhookSecret,
		BotUserID:          cfg.GitLab.BotUserID,
		ReviewTriggerLabel: cfg.Review.TriggerLabel,
		RepoSettings:       do.MustInvoke[domain.RepositorySettingsStore](i),
		ReviewJobStore:     do.MustInvoke[domain.ReviewJobStore](i),
		ReplyJobStore:      do.MustInvoke[domain.ReplyJobStore](i),
		GitLabClient:       do.MustInvoke[domain.GitLabClient](i),
		Queue:              do.MustInvoke[*queue.Queue](i),
	}), nil
}
