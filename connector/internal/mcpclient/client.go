package mcpclient

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
	"github.com/adx-agentic-payment/adx/connector/internal/store"
)

const (
	protocolVersion  = "2026-07-28"
	maxResponseBytes = 1024 * 1024
)

type TaskAvailable struct {
	WakeID       string    `json:"wake_id"`
	TaskID       string    `json:"task_id"`
	BindingID    string    `json:"binding_id"`
	BindingEpoch uint64    `json:"binding_epoch"`
	DeadlineAt   time.Time `json:"deadline_at"`
}

func (w TaskAvailable) Validate(now time.Time) error {
	if strings.TrimSpace(w.WakeID) == "" ||
		strings.TrimSpace(w.TaskID) == "" ||
		strings.TrimSpace(w.BindingID) == "" {
		return errors.New("task.available identifiers are required")
	}
	if w.BindingEpoch == 0 {
		return errors.New("task.available binding_epoch is required")
	}
	if w.DeadlineAt.IsZero() || !now.Before(w.DeadlineAt) {
		return errors.New("task.available deadline has expired")
	}
	return nil
}

type BindingRef struct {
	BindingID    string `json:"binding_id"`
	BindingEpoch uint64 `json:"binding_epoch"`
}

func (b BindingRef) Validate() error {
	if strings.TrimSpace(b.BindingID) == "" {
		return errors.New("MCP binding_id is required")
	}
	if b.BindingEpoch == 0 {
		return errors.New("MCP binding_epoch is required")
	}
	return nil
}

type TaskHint struct {
	TaskID       string    `json:"taskId"`
	BindingID    string    `json:"bindingId"`
	BindingEpoch uint64    `json:"bindingEpoch"`
	DeadlineAt   time.Time `json:"deadlineAt"`
	Status       string    `json:"status"`
}

type SyncPage struct {
	Tasks      []TaskHint `json:"tasks"`
	HasMore    bool       `json:"hasMore"`
	NextCursor *string    `json:"nextCursor"`
}

type ExecutionRoute struct {
	BindingID    string `json:"bindingId"`
	BindingEpoch uint64 `json:"bindingEpoch"`
	AgentID      string `json:"agentId"`
	RuntimeID    string `json:"runtimeId"`
	SessionID    string `json:"sessionId"`
}

type Task struct {
	TaskID         string    `json:"taskId"`
	IdempotencyKey string    `json:"idempotencyKey"`
	DeadlineAt     time.Time `json:"deadlineAt"`
}

type Claim struct {
	LeaseID   string          `json:"leaseId"`
	Task      json.RawMessage `json:"task"`
	Execution ExecutionRoute  `json:"execution"`
}

func (c Claim) DecodeCommand() (protocol.Command, error) {
	var task Task
	if err := json.Unmarshal(c.Task, &task); err != nil {
		return protocol.Command{}, fmt.Errorf("decode claimed Arena task: %w", err)
	}
	if task.TaskID == "" || task.IdempotencyKey == "" || task.DeadlineAt.IsZero() {
		return protocol.Command{}, errors.New("claimed Arena task is incomplete")
	}
	payload, err := json.Marshal(map[string]json.RawMessage{"task": c.Task})
	if err != nil {
		return protocol.Command{}, fmt.Errorf("encode claimed Arena task: %w", err)
	}
	command := protocol.Command{
		CommandID:      "mcp-task-" + task.TaskID,
		BindingID:      c.Execution.BindingID,
		AgentID:        c.Execution.AgentID,
		Kind:           protocol.CommandTaskDispatch,
		IdempotencyKey: task.IdempotencyKey,
		RuntimeID:      c.Execution.RuntimeID,
		SessionID:      c.Execution.SessionID,
		BindingEpoch:   c.Execution.BindingEpoch,
		ExpiresAt:      task.DeadlineAt,
		Payload:        payload,
	}
	if err := command.Validate(time.Now().UTC()); err != nil {
		return protocol.Command{}, fmt.Errorf("validate claimed Arena task: %w", err)
	}
	return command, nil
}

type SubmissionReceipt struct {
	TaskID      string `json:"taskId"`
	ResultID    string `json:"resultId"`
	Disposition string `json:"disposition"`
	TaskStatus  string `json:"taskStatus"`
}

type ToolError struct {
	Message string
}

func (e *ToolError) Error() string {
	return e.Message
}

type Client struct {
	httpClient       *http.Client
	mcpEndpoint      string
	tokenEndpoint    string
	credentials      store.Credentials
	connectorVersion string
}

func New(credentials store.Credentials, connectorVersion string) (*Client, error) {
	if err := credentials.Validate(); err != nil {
		return nil, err
	}
	origin, err := httpOrigin(credentials.GatewayURL)
	if err != nil {
		return nil, err
	}
	return &Client{
		httpClient: &http.Client{
			Timeout: 20 * time.Second,
		},
		mcpEndpoint:      origin + "/mcp",
		tokenEndpoint:    origin + "/api/connectors/mcp/token",
		credentials:      credentials,
		connectorVersion: connectorVersion,
	}, nil
}

func (c *Client) Claim(ctx context.Context, wake TaskAvailable) (Claim, error) {
	if err := wake.Validate(time.Now().UTC()); err != nil {
		return Claim{}, err
	}
	token, epoch, err := c.exchangeToken(ctx, wake.BindingID)
	if err != nil {
		return Claim{}, err
	}
	if epoch != wake.BindingEpoch {
		return Claim{}, errors.New("execution token binding epoch does not match wake")
	}
	var claim Claim
	if err := c.callTool(
		ctx,
		token,
		"arena_claim_agent_task",
		map[string]any{"taskId": wake.TaskID},
		&claim,
	); err != nil {
		return Claim{}, err
	}
	if claim.Execution.BindingID != wake.BindingID ||
		claim.Execution.BindingEpoch != wake.BindingEpoch {
		return Claim{}, errors.New("claimed task execution route does not match wake")
	}
	return claim, nil
}

func (c *Client) Submit(
	ctx context.Context,
	envelope protocol.AgentTaskResultEnvelope,
) (SubmissionReceipt, error) {
	if err := envelope.Validate(); err != nil {
		return SubmissionReceipt{}, err
	}
	token, epoch, err := c.exchangeToken(ctx, envelope.BindingID)
	if err != nil {
		return SubmissionReceipt{}, err
	}
	if epoch != envelope.BindingEpoch {
		return SubmissionReceipt{}, errors.New(
			"execution token binding epoch does not match durable result",
		)
	}
	var receipt SubmissionReceipt
	if err := c.callTool(
		ctx,
		token,
		"arena_submit_agent_task_result",
		map[string]any{"result": envelope.Result},
		&receipt,
	); err != nil {
		return SubmissionReceipt{}, err
	}
	if receipt.TaskID != envelope.Result.TaskID ||
		receipt.ResultID != envelope.Result.ResultID {
		return SubmissionReceipt{}, errors.New(
			"Arena MCP result acknowledgement does not match submitted result",
		)
	}
	return receipt, nil
}

func (c *Client) Release(
	ctx context.Context,
	wake TaskAvailable,
) error {
	token, epoch, err := c.exchangeToken(ctx, wake.BindingID)
	if err != nil {
		return err
	}
	if epoch != wake.BindingEpoch {
		return errors.New("execution token binding epoch does not match wake")
	}
	var released struct {
		TaskID   string `json:"taskId"`
		Released bool   `json:"released"`
	}
	if err := c.callTool(
		ctx,
		token,
		"arena_release_agent_task",
		map[string]any{"taskId": wake.TaskID},
		&released,
	); err != nil {
		return err
	}
	if released.TaskID != wake.TaskID || !released.Released {
		return errors.New("Arena MCP release acknowledgement is invalid")
	}
	return nil
}

func (c *Client) Sync(
	ctx context.Context,
	binding BindingRef,
	cursor *string,
	limit int,
) (SyncPage, error) {
	if err := binding.Validate(); err != nil {
		return SyncPage{}, err
	}
	if limit < 1 || limit > 50 {
		return SyncPage{}, errors.New("Arena MCP sync limit must be between 1 and 50")
	}
	token, epoch, err := c.exchangeToken(ctx, binding.BindingID)
	if err != nil {
		return SyncPage{}, err
	}
	if epoch != binding.BindingEpoch {
		return SyncPage{}, errors.New(
			"execution token binding epoch does not match sync binding",
		)
	}
	arguments := map[string]any{"limit": limit}
	if cursor != nil {
		arguments["cursor"] = *cursor
	}
	var page SyncPage
	if err := c.callTool(
		ctx,
		token,
		"arena_sync_agent_tasks",
		arguments,
		&page,
	); err != nil {
		return SyncPage{}, err
	}
	if len(page.Tasks) > limit {
		return SyncPage{}, errors.New("Arena MCP sync exceeded the requested limit")
	}
	for _, task := range page.Tasks {
		if strings.TrimSpace(task.TaskID) == "" ||
			task.BindingID != binding.BindingID ||
			task.BindingEpoch != binding.BindingEpoch ||
			task.DeadlineAt.IsZero() ||
			!time.Now().UTC().Before(task.DeadlineAt) ||
			(task.Status != "queued" && task.Status != "leased") {
			return SyncPage{}, errors.New("Arena MCP sync returned an invalid task hint")
		}
	}
	if page.HasMore && (page.NextCursor == nil || *page.NextCursor == "") {
		return SyncPage{}, errors.New("Arena MCP sync omitted the next cursor")
	}
	if !page.HasMore && page.NextCursor != nil {
		return SyncPage{}, errors.New("Arena MCP sync returned an unexpected cursor")
	}
	return page, nil
}

func (c *Client) exchangeToken(
	ctx context.Context,
	bindingID string,
) (string, uint64, error) {
	body, err := json.Marshal(map[string]string{
		"deviceId":  c.credentials.DeviceID,
		"bindingId": bindingID,
	})
	if err != nil {
		return "", 0, err
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		c.tokenEndpoint,
		bytes.NewReader(body),
	)
	if err != nil {
		return "", 0, err
	}
	request.Header.Set("Authorization", "Device "+c.credentials.Token)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.httpClient.Do(request)
	if err != nil {
		return "", 0, fmt.Errorf("exchange Arena MCP execution token: %w", err)
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return "", 0, fmt.Errorf("read Arena MCP token response: %w", err)
	}
	if len(raw) > maxResponseBytes {
		return "", 0, errors.New("Arena MCP token response exceeded size limit")
	}
	if response.StatusCode != http.StatusOK {
		return "", 0, fmt.Errorf(
			"Arena MCP token exchange returned HTTP %d",
			response.StatusCode,
		)
	}
	var payload struct {
		AccessToken  string `json:"access_token"`
		TokenType    string `json:"token_type"`
		BindingID    string `json:"binding_id"`
		BindingEpoch uint64 `json:"binding_epoch"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", 0, fmt.Errorf("decode Arena MCP token response: %w", err)
	}
	if payload.AccessToken == "" ||
		!strings.EqualFold(payload.TokenType, "Bearer") ||
		payload.BindingID != bindingID ||
		payload.BindingEpoch == 0 {
		return "", 0, errors.New("Arena MCP token response is invalid")
	}
	return payload.AccessToken, payload.BindingEpoch, nil
}

func (c *Client) callTool(
	ctx context.Context,
	token string,
	name string,
	arguments map[string]any,
	output any,
) error {
	requestID := protocol.NewID("mcp-request")
	body, err := json.Marshal(map[string]any{
		"jsonrpc": "2.0",
		"id":      requestID,
		"method":  "tools/call",
		"params": map[string]any{
			"name":      name,
			"arguments": arguments,
			"_meta": map[string]any{
				"io.modelcontextprotocol/protocolVersion": protocolVersion,
				"io.modelcontextprotocol/clientInfo": map[string]string{
					"name":    "adx-connector",
					"version": c.connectorVersion,
				},
				"io.modelcontextprotocol/clientCapabilities": map[string]any{},
			},
		},
	})
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		c.mcpEndpoint,
		bytes.NewReader(body),
	)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json, text/event-stream")
	request.Header.Set("MCP-Protocol-Version", protocolVersion)
	request.Header.Set("Mcp-Method", "tools/call")
	request.Header.Set("Mcp-Name", name)
	response, err := c.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("call Arena MCP tool %s: %w", name, err)
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes+1))
	if err != nil {
		return fmt.Errorf("read Arena MCP tool response: %w", err)
	}
	if len(raw) > maxResponseBytes {
		return errors.New("Arena MCP tool response exceeded size limit")
	}
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf(
			"Arena MCP tool %s returned HTTP %d",
			name,
			response.StatusCode,
		)
	}
	var rpc struct {
		JSONRPC string `json:"jsonrpc"`
		ID      string `json:"id"`
		Error   *struct {
			Code    int    `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
		Result struct {
			IsError           bool            `json:"isError"`
			Content           []content       `json:"content"`
			StructuredContent json.RawMessage `json:"structuredContent"`
		} `json:"result"`
	}
	if err := json.Unmarshal(raw, &rpc); err != nil {
		return fmt.Errorf("decode Arena MCP tool response: %w", err)
	}
	if rpc.JSONRPC != "2.0" || rpc.ID != requestID {
		return errors.New("Arena MCP tool response correlation failed")
	}
	if rpc.Error != nil {
		return fmt.Errorf(
			"Arena MCP protocol error %d: %s",
			rpc.Error.Code,
			rpc.Error.Message,
		)
	}
	if rpc.Result.IsError {
		message := "Arena MCP tool rejected the operation"
		if len(rpc.Result.Content) != 0 && rpc.Result.Content[0].Text != "" {
			message = rpc.Result.Content[0].Text
		}
		return &ToolError{Message: message}
	}
	if len(rpc.Result.StructuredContent) == 0 {
		return errors.New("Arena MCP tool response omitted structuredContent")
	}
	if err := json.Unmarshal(rpc.Result.StructuredContent, output); err != nil {
		return fmt.Errorf("decode Arena MCP structuredContent: %w", err)
	}
	return nil
}

type content struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

func httpOrigin(raw string) (string, error) {
	parsed, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("parse gateway URL: %w", err)
	}
	switch strings.ToLower(parsed.Scheme) {
	case "wss":
		parsed.Scheme = "https"
	case "ws":
		parsed.Scheme = "http"
	case "https", "http":
	default:
		return "", errors.New("gateway URL must use ws, wss, http, or https")
	}
	if parsed.Hostname() == "" {
		return "", errors.New("gateway URL host is required")
	}
	if parsed.Scheme == "http" && !isLoopback(parsed.Hostname()) {
		return "", errors.New("unencrypted Arena MCP is allowed only for localhost")
	}
	parsed.Path = ""
	parsed.RawPath = ""
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return strings.TrimRight(parsed.String(), "/"), nil
}

func isLoopback(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback()
}
