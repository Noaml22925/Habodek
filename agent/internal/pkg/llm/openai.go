package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"habodek/internal/domain"
)

type OpenAIClient struct {
	apiKey        string
	baseURL       string
	model         string
	contextWindow int
	seed          *int
	httpClient    *http.Client
}

// WithSeed pins the sampling seed sent with every request, or clears it when
// given nil.
//
// Reviews run at temperature 0.1, which is low but not zero, so the same merge
// request reviewed twice does not produce the same findings. Measured on the
// corpus, two runs of an identical configuration differed by three detections
// and by one false positive on a clean case -- more than any prompt change we
// have made. Without a seed the evaluation harness cannot attribute a
// difference to the edit that supposedly caused it.
//
// Off by default: an unset seed is omitted from the request entirely, so
// nothing changes for a gateway that does not support it. Turn it on for
// evaluation runs, not for production reviews, where varied sampling is
// harmless and determinism buys nothing.
//
// It does not deliver reproducible reviews against Ollama, which is measured,
// not assumed. The seed reaches the wire (the request body grows by exactly
// `,"seed":N`) and Ollama applies it -- its native API returns byte-identical
// completions across four calls at temperature 0.8. But the OpenAI-compatible
// endpoint reproduced only three of four, and a full review, which is several
// calls deep with tool use in between, came back differently worded on two runs
// with the same seed. Treat this as variance reduction, not determinism, and do
// not let it replace running a configuration more than once. It may behave
// better on the real gateway; verify there rather than inheriting this result.
func (c *OpenAIClient) WithSeed(seed *int) *OpenAIClient {
	c.seed = seed
	return c
}

func NewOpenAIClient(apiKey, baseURL, model string, contextWindow int) (*OpenAIClient, error) {
	if apiKey == "" {
		return nil, fmt.Errorf("OpenAI API key is required")
	}
	if baseURL == "" {
		baseURL = "https://api.openai.com/v1"
	}
	return &OpenAIClient{
		apiKey:        apiKey,
		baseURL:       baseURL,
		model:         model,
		contextWindow: contextWindow,
		httpClient:    &http.Client{Timeout: 5 * time.Minute},
	}, nil
}

func (c *OpenAIClient) ModelName() string      { return c.model }
func (c *OpenAIClient) ContextWindowSize() int { return c.contextWindow }
func (c *OpenAIClient) ClientCount() int       { return 1 }

func (c *OpenAIClient) Chat(ctx context.Context, req domain.ChatRequest) (*domain.ChatResponse, error) {
	// Build messages
	var messages []map[string]any
	for _, msg := range req.Messages {
		switch msg.Role {
		case "system":
			messages = append(messages, map[string]any{"role": "system", "content": msg.Content})
		case "user":
			messages = append(messages, map[string]any{"role": "user", "content": msg.Content})
		case "assistant":
			m := map[string]any{"role": "assistant"}
			if msg.Content != "" {
				m["content"] = msg.Content
			}
			if len(msg.ToolCalls) > 0 {
				var tcs []map[string]any
				for _, tc := range msg.ToolCalls {
					tcs = append(tcs, map[string]any{
						"id":   tc.ID,
						"type": "function",
						"function": map[string]any{
							"name":      tc.Name,
							"arguments": tc.InputJSON,
						},
					})
				}
				m["tool_calls"] = tcs
			}
			messages = append(messages, m)
		case "tool":
			messages = append(messages, map[string]any{
				"role":         "tool",
				"content":      msg.Content,
				"tool_call_id": msg.ToolCallID,
			})
		}
	}

	body := map[string]any{
		"model":    c.model,
		"messages": messages,
	}
	if req.MaxTokens > 0 {
		body["max_completion_tokens"] = req.MaxTokens
	}
	if req.Temperature > 0 {
		body["temperature"] = req.Temperature
	}
	if c.seed != nil {
		body["seed"] = *c.seed
	}

	// Build tools
	if len(req.Tools) > 0 {
		var tools []map[string]any
		for _, t := range req.Tools {
			tools = append(tools, map[string]any{
				"type": "function",
				"function": map[string]any{
					"name":        t.Name,
					"description": t.Description,
					"parameters":  t.InputSchema,
				},
			})
		}
		body["tools"] = tools
	}

	data, err := json.Marshal(body)
	if err != nil {
		return nil, fmt.Errorf("marshal request: %w", err)
	}

	respBody, err := doRequest(ctx, c.httpClient, "POST", c.baseURL+"/chat/completions", data,
		map[string]string{
			"Content-Type":  "application/json",
			"Authorization": "Bearer " + c.apiKey,
		})
	if err != nil {
		return nil, err
	}

	slog.Debug("LLM Chat Prompt Payload", "url", c.baseURL+"/chat/completions", "payload_size", len(data))

	var apiResp struct {
		Choices []struct {
			Message struct {
				Content   string `json:"content"`
				ToolCalls []struct {
					ID       string `json:"id"`
					Type     string `json:"type"`
					Function struct {
						Name      string `json:"name"`
						Arguments string `json:"arguments"`
					} `json:"function"`
				} `json:"tool_calls"`
			} `json:"message"`
			FinishReason string `json:"finish_reason"`
		} `json:"choices"`
		Usage struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(respBody, &apiResp); err != nil {
		return nil, fmt.Errorf("unmarshal response: %w", err)
	}

	result := &domain.ChatResponse{
		Usage: domain.TokenUsage{
			InputTokens:  apiResp.Usage.PromptTokens,
			OutputTokens: apiResp.Usage.CompletionTokens,
		},
	}

	if len(apiResp.Choices) > 0 {
		choice := apiResp.Choices[0]
		result.Content = choice.Message.Content
		result.StopReason = choice.FinishReason

		for _, tc := range choice.Message.ToolCalls {
			result.ToolCalls = append(result.ToolCalls, domain.ToolCall{
				ID:        tc.ID,
				Name:      tc.Function.Name,
				InputJSON: tc.Function.Arguments,
			})
		}
	}

	return result, nil
}
