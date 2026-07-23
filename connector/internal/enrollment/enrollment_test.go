package enrollment

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestPairWaitsForApprovalAndReturnsCredentials(t *testing.T) {
	var exchanges atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		switch request.URL.Path {
		case "/api/connectors/pairings":
			response.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(response).Encode(map[string]any{
				"device_code":      "device-code",
				"user_code":        "ABCD-EFGH",
				"verification_uri": serverURL(request) + "/connect",
				"expires_in":       30,
				"interval":         1,
			})
		case "/api/connectors/pairings/exchange":
			if exchanges.Add(1) == 1 {
				response.WriteHeader(http.StatusPreconditionRequired)
				return
			}
			_ = json.NewEncoder(response).Encode(map[string]any{
				"device_id":    "device-1",
				"device_token": "token-1",
				"ws_url":       "/api/connectors/ws",
			})
		default:
			http.NotFound(response, request)
		}
	}))
	defer server.Close()

	var output bytes.Buffer
	client := &Client{
		BaseURL:          server.URL,
		ConnectorVersion: "test",
		HTTPClient:       &http.Client{Timeout: 2 * time.Second},
		Output:           &output,
	}
	credentials, err := client.Pair(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if credentials.DeviceID != "device-1" || credentials.Token != "token-1" {
		t.Fatalf("unexpected credentials: %#v", credentials)
	}
	if !strings.HasPrefix(credentials.GatewayURL, "ws://") {
		t.Fatalf("unexpected gateway URL: %s", credentials.GatewayURL)
	}
	if !strings.HasSuffix(credentials.GatewayURL, "/api/connectors/ws") {
		t.Fatalf("relative ws_url was not resolved: %s", credentials.GatewayURL)
	}
	if !strings.Contains(output.String(), "ABCD-EFGH") {
		t.Fatalf("user code was not displayed: %s", output.String())
	}
}

func TestValidateBaseURLRejectsRemotePlaintextHTTP(t *testing.T) {
	if _, err := validateBaseURL("http://example.com"); err == nil {
		t.Fatal("remote plaintext enrollment must be rejected")
	}
	if _, err := validateBaseURL("http://127.0.0.1:8000"); err != nil {
		t.Fatalf("localhost development URL should be allowed: %v", err)
	}
}

func serverURL(request *http.Request) string {
	return "http://" + request.Host
}
