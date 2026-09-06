package replier

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"habodek/internal/core/prompt"
	"habodek/internal/domain"
)

const (
	replyMaxTokens   = 600
	replyTemperature = 0.3
)

type Agent struct {
}

func NewAgent() *Agent {
	return &Agent{}
}

type ReplyInput struct {
	Job               *domain.ReplyJob
	MR                *domain.GitLabMR
	ThreadHistory     string
	CodeContext       string
	LatestCodeContext string
	CustomPrompt      *string
	Intent            domain.ReplyIntent
	DetectedLang      string
	ResponseLanguage  prompt.ResponseLanguage
}

// VerdictInput drives the re-check pass: no developer reply to answer, just an
// open finding and the code as it stands now.
type VerdictInput struct {
	MR                *domain.GitLabMR
	FilePath          string
	LineNumber        int
	OriginalComment   string
	ThreadHistory     string
	LatestCodeContext string
	CustomPrompt      *string
	ResponseLanguage  prompt.ResponseLanguage
}

// ReplyResult is what the reviewer concluded, plus the prose it wants posted.
// Verdict is decided by the model reading the code and the argument, not by
// keyword-matching the developer's words.
type ReplyResult struct {
	Verdict domain.ReplyVerdict
	Reply   string
	// VerdictParsed is false when the model did not return usable JSON and the
	// caller must fall back to its own classification. The prose is still good.
	VerdictParsed bool
}

// GenerateReply makes a single LLM call to answer a developer's reply.
func (a *Agent) GenerateReply(ctx context.Context, llmClient domain.LLMClient, input ReplyInput) (ReplyResult, error) {
	systemPrompt := a.buildSystemPrompt(input)
	userMessage := a.buildUserMessage(input)

	content, err := a.chat(ctx, llmClient, systemPrompt, userMessage)
	if err != nil {
		return ReplyResult{}, fmt.Errorf("reply LLM call: %w", err)
	}
	return parseReplyResult(content), nil
}

// GenerateVerdict re-checks one open finding against the current code and says
// whether it still stands. Used by the review pipeline after a push.
func (a *Agent) GenerateVerdict(ctx context.Context, llmClient domain.LLMClient, input VerdictInput) (ReplyResult, error) {
	systemPrompt := prompt.ThreadVerdictSystemPrompt(input.ResponseLanguage)
	if input.CustomPrompt != nil && *input.CustomPrompt != "" {
		systemPrompt += "\n## Project-Specific Instructions\n" + *input.CustomPrompt + "\n"
	}

	content, err := a.chat(ctx, llmClient, systemPrompt, buildVerdictUserMessage(input))
	if err != nil {
		return ReplyResult{}, fmt.Errorf("verdict LLM call: %w", err)
	}
	return parseReplyResult(content), nil
}

func (a *Agent) chat(ctx context.Context, llmClient domain.LLMClient, systemPrompt, userMessage string) (string, error) {
	req := domain.ChatRequest{
		Model:       llmClient.ModelName(),
		MaxTokens:   replyMaxTokens,
		Temperature: replyTemperature,
		Messages: []domain.ChatMessage{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: userMessage},
		},
	}

	resp, err := llmClient.Chat(ctx, req)
	if err != nil {
		return "", err
	}
	return resp.Content, nil
}

type rawReplyResult struct {
	Verdict string `json:"verdict"`
	Reply   string `json:"reply"`
}

// parseReplyResult pulls {"verdict": ..., "reply": ...} out of the model
// output. Anything unparseable is returned as prose with VerdictParsed false,
// so a model that ignores the format still produces a usable comment.
func parseReplyResult(content string) ReplyResult {
	trimmed := strings.TrimSpace(content)
	if trimmed == "" {
		return ReplyResult{Reply: ""}
	}

	candidate := stripCodeFence(trimmed)
	start := strings.Index(candidate, "{")
	end := strings.LastIndex(candidate, "}")
	if start >= 0 && end > start {
		var raw rawReplyResult
		if err := json.Unmarshal([]byte(candidate[start:end+1]), &raw); err == nil {
			verdict := domain.ReplyVerdict(strings.ToLower(strings.TrimSpace(raw.Verdict)))
			reply := strings.TrimSpace(raw.Reply)
			if domain.ValidReplyVerdict(verdict) && reply != "" {
				return ReplyResult{Verdict: verdict, Reply: reply, VerdictParsed: true}
			}
			// Valid JSON but not the shape we asked for: keep whichever half
			// is usable rather than posting a raw JSON blob into the thread.
			if reply != "" {
				return ReplyResult{Reply: reply}
			}
		}
	}

	return ReplyResult{Reply: trimmed}
}

func stripCodeFence(s string) string {
	if !strings.HasPrefix(s, "```") {
		return s
	}
	if idx := strings.Index(s, "\n"); idx >= 0 {
		s = s[idx+1:]
	}
	return strings.TrimSuffix(strings.TrimSpace(s), "```")
}

func (a *Agent) buildSystemPrompt(input ReplyInput) string {
	base := prompt.ReplierSystemPrompt(input.ResponseLanguage)
	if input.CustomPrompt != nil && *input.CustomPrompt != "" {
		base += "\n## Project-Specific Instructions\n" + *input.CustomPrompt + "\n"
	}
	return base
}

func (a *Agent) buildUserMessage(input ReplyInput) string {
	var sb strings.Builder

	if input.MR != nil {
		sb.WriteString(fmt.Sprintf("## MR Context\nTitle: %s\n", input.MR.Title))
		desc := input.MR.Description
		if len(desc) > 200 {
			desc = desc[:200] + "..."
		}
		if desc != "" {
			sb.WriteString(fmt.Sprintf("Description: %s\n", desc))
		}
		sb.WriteString("\n")
	}

	sb.WriteString("## Original Review Comment\n")
	if input.Job.BotCommentFilePath != nil {
		sb.WriteString(fmt.Sprintf("File: %s", *input.Job.BotCommentFilePath))
		if input.Job.BotCommentLine != nil {
			sb.WriteString(fmt.Sprintf(":%d", *input.Job.BotCommentLine))
		}
		sb.WriteString("\n")
	}
	sb.WriteString(fmt.Sprintf("Comment: %s\n\n", input.Job.BotCommentContent))

	if input.LatestCodeContext != "" {
		sb.WriteString("## Latest Code (HEAD) — use this to verify if the issue was actually fixed\n```\n")
		sb.WriteString(input.LatestCodeContext)
		sb.WriteString("\n```\n\n")
	} else if input.CodeContext != "" {
		sb.WriteString("## Relevant Code Around Comment\n```\n")
		sb.WriteString(input.CodeContext)
		sb.WriteString("\n```\n\n")
	}

	sb.WriteString("## Discussion Thread\n")
	sb.WriteString(input.ThreadHistory)
	sb.WriteString("\n\n")

	sb.WriteString(fmt.Sprintf("## Developer's Reply (respond to this)\n%s\n\n", input.Job.TriggerNoteContent))
	sb.WriteString(fmt.Sprintf("## Keyword Hint (weak signal, may be wrong — judge the reply yourself)\nThe developer may be: %s\n", input.Intent))

	return sb.String()
}

func buildVerdictUserMessage(input VerdictInput) string {
	var sb strings.Builder

	if input.MR != nil {
		sb.WriteString(fmt.Sprintf("## MR Context\nTitle: %s\n\n", input.MR.Title))
	}

	sb.WriteString("## Your Original Review Comment\n")
	sb.WriteString(fmt.Sprintf("File: %s:%d\n", input.FilePath, input.LineNumber))
	sb.WriteString(fmt.Sprintf("Comment: %s\n\n", input.OriginalComment))

	if input.ThreadHistory != "" {
		sb.WriteString("## Discussion So Far\n")
		sb.WriteString(input.ThreadHistory)
		sb.WriteString("\n\n")
	}

	if input.LatestCodeContext != "" {
		sb.WriteString("## Current Code at HEAD\n```\n")
		sb.WriteString(input.LatestCodeContext)
		sb.WriteString("\n```\n\n")
	} else {
		sb.WriteString("## Current Code at HEAD\nUnavailable — the file could not be read at this revision.\n\n")
	}

	sb.WriteString("Decide whether your original finding still stands against the code above.\n")
	return sb.String()
}
