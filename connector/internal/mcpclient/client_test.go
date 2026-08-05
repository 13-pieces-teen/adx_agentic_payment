package mcpclient

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/protocol"
	"github.com/adx-agentic-payment/adx/connector/internal/store"
)

func TestClaimAndSubmitUseStatelessMCPHeadersAndBindingToken(t *testing.T) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/api/connectors/mcp/token":
			if request.Header.Get("Authorization") != "Device device-token" {
				t.Errorf("unexpected device authorization: %q", request.Header.Get("Authorization"))
			}
			var body struct {
				DeviceID  string `json:"deviceId"`
				BindingID string `json:"bindingId"`
			}
			if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
				t.Errorf("decode token request: %v", err)
				response.WriteHeader(http.StatusBadRequest)
				return
			}
			if body.DeviceID != "device-1" || body.BindingID != "binding-1" {
				t.Errorf("unexpected token authority: %#v", body)
			}
			_ = json.NewEncoder(response).Encode(map[string]any{
				"access_token":  "execution-token",
				"token_type":    "Bearer",
				"binding_id":    "binding-1",
				"binding_epoch": 7,
			})
		case "/mcp":
			if request.Header.Get("Authorization") != "Bearer execution-token" {
				t.Errorf("unexpected MCP authorization")
			}
			if request.Header.Get("MCP-Protocol-Version") != protocolVersion ||
				request.Header.Get("Mcp-Method") != "tools/call" {
				t.Errorf("missing stateless MCP headers")
			}
			accept := request.Header.Get("Accept")
			if !strings.Contains(accept, "application/json") ||
				!strings.Contains(accept, "text/event-stream") {
				t.Errorf("unexpected Accept header: %q", accept)
			}
			var rpc struct {
				ID     string `json:"id"`
				Method string `json:"method"`
				Params struct {
					Name string `json:"name"`
				} `json:"params"`
			}
			if err := json.NewDecoder(request.Body).Decode(&rpc); err != nil {
				t.Errorf("decode MCP request: %v", err)
				response.WriteHeader(http.StatusBadRequest)
				return
			}
			if rpc.Method != "tools/call" || request.Header.Get("Mcp-Name") != rpc.Params.Name {
				t.Errorf("MCP method/name header mismatch")
			}
			var structured any
			switch rpc.Params.Name {
			case "arena_claim_agent_task":
				structured = map[string]any{
					"leaseId": "mcp-lease-1",
					"task": map[string]any{
						"taskId":         "task-1",
						"idempotencyKey": "game-1:round-1:agent-1:decide",
						"deadlineAt":     time.Now().Add(time.Minute).UTC(),
					},
					"execution": map[string]any{
						"bindingId":    "binding-1",
						"bindingEpoch": 7,
						"agentId":      "agent-1",
						"runtimeId":    "codex",
						"sessionId":    "session-1",
					},
				}
			case "arena_submit_agent_task_result":
				structured = map[string]any{
					"taskId":      "task-1",
					"resultId":    "result-1",
					"disposition": "accepted",
					"taskStatus":  "completed",
				}
			case "arena_sync_agent_tasks":
				structured = map[string]any{
					"tasks": []any{
						map[string]any{
							"taskId":       "task-1",
							"bindingId":    "binding-1",
							"bindingEpoch": 7,
							"deadlineAt":   time.Now().Add(time.Minute).UTC(),
							"status":       "queued",
						},
					},
					"hasMore":    false,
					"nextCursor": nil,
				}
			default:
				t.Errorf("unexpected tool %q", rpc.Params.Name)
				response.WriteHeader(http.StatusBadRequest)
				return
			}
			_ = json.NewEncoder(response).Encode(map[string]any{
				"jsonrpc": "2.0",
				"id":      rpc.ID,
				"result": map[string]any{
					"resultType":        "complete",
					"content":           []any{},
					"structuredContent": structured,
					"isError":           false,
				},
			})
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()

	client, err := New(
		store.Credentials{
			DeviceID:   "device-1",
			Token:      "device-token",
			GatewayURL: "ws" + strings.TrimPrefix(server.URL, "http"),
		},
		"test-version",
	)
	if err != nil {
		t.Fatal(err)
	}
	wake := TaskAvailable{
		WakeID:       "wake:task-1:7",
		TaskID:       "task-1",
		BindingID:    "binding-1",
		BindingEpoch: 7,
		DeadlineAt:   time.Now().Add(time.Minute).UTC(),
	}
	claim, err := client.Claim(context.Background(), wake)
	if err != nil {
		t.Fatal(err)
	}
	command, err := claim.DecodeCommand()
	if err != nil {
		t.Fatal(err)
	}
	if command.CommandKind() != protocol.CommandTaskDispatch ||
		command.BindingID != wake.BindingID ||
		command.BindingEpoch != wake.BindingEpoch {
		t.Fatalf("unexpected claimed command: %#v", command)
	}

	receipt, err := client.Submit(
		context.Background(),
		protocol.AgentTaskResultEnvelope{
			BindingID:    "binding-1",
			BindingEpoch: 7,
			Result: protocol.AgentTaskResult{
				SchemaVersion: "arena.agent-result.v1",
				ResultID:      "result-1",
				TaskID:        "task-1",
				Status:        "succeeded",
				Action:        json.RawMessage(`{"action":"pass"}`),
			},
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Disposition != "accepted" || receipt.TaskStatus != "completed" {
		t.Fatalf("unexpected receipt: %#v", receipt)
	}

	page, err := client.Sync(
		context.Background(),
		BindingRef{BindingID: "binding-1", BindingEpoch: 7},
		nil,
		50,
	)
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Tasks) != 1 || page.Tasks[0].TaskID != "task-1" {
		t.Fatalf("unexpected sync page: %#v", page)
	}
}

func TestClaimCommandIDChangesWithRecoveredManagedSession(t *testing.T) {
	deadline := time.Now().Add(time.Minute).UTC()
	task := json.RawMessage(
		`{"taskId":"task-1","idempotencyKey":"idem-1","deadlineAt":"` +
			deadline.Format(time.RFC3339Nano) + `"}`,
	)
	beforeRestart := Claim{
		Task: task,
		Execution: ExecutionRoute{
			BindingID:    "binding-1",
			BindingEpoch: 7,
			AgentID:      "agent-1",
			RuntimeID:    "runtime-1",
			SessionID:    "session-before-restart",
		},
	}
	afterRestart := beforeRestart
	afterRestart.Execution.SessionID = "session-after-restart"

	first, err := beforeRestart.DecodeCommand()
	if err != nil {
		t.Fatal(err)
	}
	replayed, err := beforeRestart.DecodeCommand()
	if err != nil {
		t.Fatal(err)
	}
	recovered, err := afterRestart.DecodeCommand()
	if err != nil {
		t.Fatal(err)
	}
	if first.CommandID != replayed.CommandID {
		t.Fatalf(
			"same task and session must retain a stable command id: %q != %q",
			first.CommandID,
			replayed.CommandID,
		)
	}
	if first.CommandID == recovered.CommandID {
		t.Fatalf(
			"recovered session must receive a new command id: %q",
			first.CommandID,
		)
	}
}

func TestNewRejectsRemotePlaintextMCPOrigin(t *testing.T) {
	_, err := New(
		store.Credentials{
			DeviceID:   "device-1",
			Token:      "device-token",
			GatewayURL: "ws://arena.example/api/connectors/ws",
		},
		"test-version",
	)
	if err == nil || !strings.Contains(err.Error(), "only for localhost") {
		t.Fatalf("remote plaintext MCP origin should be rejected, got %v", err)
	}
}
