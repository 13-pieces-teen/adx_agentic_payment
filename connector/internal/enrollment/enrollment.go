package enrollment

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
	"runtime"
	"strings"
	"time"

	"github.com/adx-agentic-payment/adx/connector/internal/store"
)

const maxResponseBytes = 1024 * 1024

type Client struct {
	BaseURL          string
	ConnectorVersion string
	HTTPClient       *http.Client
	Output           io.Writer
}

type pairingResponse struct {
	DeviceCode              string `json:"device_code"`
	UserCode                string `json:"user_code"`
	VerificationURI         string `json:"verification_uri"`
	VerificationURIComplete string `json:"verification_uri_complete"`
	ExpiresIn               int    `json:"expires_in"`
	Interval                int    `json:"interval"`
}

type exchangeResponse struct {
	DeviceID    string `json:"device_id"`
	DeviceToken string `json:"device_token"`
	Token       string `json:"token"`
	GatewayURL  string `json:"gateway_url"`
	WSURL       string `json:"ws_url"`
}

func (c *Client) Pair(ctx context.Context) (store.Credentials, error) {
	if c.HTTPClient == nil {
		c.HTTPClient = &http.Client{Timeout: 15 * time.Second}
	}
	if c.Output == nil {
		c.Output = io.Discard
	}
	baseURL, err := validateBaseURL(c.BaseURL)
	if err != nil {
		return store.Credentials{}, err
	}
	pairing, err := c.createPairing(ctx, baseURL)
	if err != nil {
		return store.Credentials{}, err
	}
	fmt.Fprintf(c.Output, "Open %s and enter code %s\n", pairing.VerificationURI, pairing.UserCode)
	if pairing.VerificationURIComplete != "" {
		fmt.Fprintf(c.Output, "Direct verification link: %s\n", pairing.VerificationURIComplete)
	}

	interval := time.Duration(pairing.Interval) * time.Second
	if interval < time.Second {
		interval = 2 * time.Second
	}
	expiresIn := pairing.ExpiresIn
	if expiresIn <= 0 {
		expiresIn = 600
	}
	pollContext, cancel := context.WithTimeout(ctx, time.Duration(expiresIn)*time.Second)
	defer cancel()

	for {
		exchanged, pending, slowDown, err := c.exchange(pollContext, baseURL, pairing.DeviceCode)
		if err != nil {
			return store.Credentials{}, err
		}
		if !pending {
			token := exchanged.DeviceToken
			if token == "" {
				token = exchanged.Token
			}
			gatewayURL := exchanged.GatewayURL
			if gatewayURL == "" && exchanged.WSURL != "" {
				gatewayURL, err = resolveGatewayURL(baseURL, exchanged.WSURL)
				if err != nil {
					return store.Credentials{}, err
				}
			}
			if gatewayURL == "" {
				gatewayURL = defaultGatewayURL(baseURL)
			}
			credentials := store.Credentials{
				DeviceID:   exchanged.DeviceID,
				Token:      token,
				GatewayURL: gatewayURL,
			}
			if err := credentials.Validate(); err != nil {
				return store.Credentials{}, fmt.Errorf("invalid pairing exchange response: %w", err)
			}
			return credentials, nil
		}
		if slowDown {
			interval += time.Second
		}
		timer := time.NewTimer(interval)
		select {
		case <-pollContext.Done():
			timer.Stop()
			return store.Credentials{}, fmt.Errorf("pairing expired or cancelled: %w", pollContext.Err())
		case <-timer.C:
		}
	}
}

func (c *Client) createPairing(ctx context.Context, baseURL *url.URL) (pairingResponse, error) {
	body := map[string]string{
		"connector_version": c.ConnectorVersion,
		"os":                runtime.GOOS,
		"architecture":      runtime.GOARCH,
	}
	var response pairingResponse
	status, err := c.postJSON(ctx, baseURL.ResolveReference(&url.URL{Path: "/api/connectors/pairings"}), body, &response)
	if err != nil {
		return pairingResponse{}, err
	}
	if status != http.StatusCreated && status != http.StatusOK {
		return pairingResponse{}, fmt.Errorf("create pairing returned HTTP %d", status)
	}
	if response.DeviceCode == "" || response.UserCode == "" || response.VerificationURI == "" {
		return pairingResponse{}, errors.New("create pairing response is missing device_code, user_code, or verification_uri")
	}
	verificationURI, err := url.Parse(response.VerificationURI)
	if err != nil {
		return pairingResponse{}, fmt.Errorf("parse verification_uri: %w", err)
	}
	response.VerificationURI = baseURL.ResolveReference(verificationURI).String()
	if response.VerificationURIComplete != "" {
		complete, err := url.Parse(response.VerificationURIComplete)
		if err != nil {
			return pairingResponse{}, fmt.Errorf("parse verification_uri_complete: %w", err)
		}
		response.VerificationURIComplete = baseURL.ResolveReference(complete).String()
	}
	return response, nil
}

func (c *Client) exchange(
	ctx context.Context,
	baseURL *url.URL,
	deviceCode string,
) (exchangeResponse, bool, bool, error) {
	var response exchangeResponse
	status, err := c.postJSON(
		ctx,
		baseURL.ResolveReference(&url.URL{Path: "/api/connectors/pairings/exchange"}),
		map[string]string{"device_code": deviceCode},
		&response,
	)
	if err != nil {
		return exchangeResponse{}, false, false, err
	}
	switch status {
	case http.StatusOK, http.StatusCreated:
		return response, false, false, nil
	case http.StatusPreconditionRequired, http.StatusAccepted:
		return exchangeResponse{}, true, false, nil
	case http.StatusTooManyRequests:
		return exchangeResponse{}, true, true, nil
	case http.StatusGone:
		return exchangeResponse{}, false, false, errors.New("pairing code expired")
	default:
		return exchangeResponse{}, false, false, fmt.Errorf("pairing exchange returned HTTP %d", status)
	}
}

func (c *Client) postJSON(ctx context.Context, endpoint *url.URL, body any, target any) (int, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return 0, err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint.String(), bytes.NewReader(encoded))
	if err != nil {
		return 0, err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "application/json")
	response, err := c.HTTPClient.Do(request)
	if err != nil {
		return 0, fmt.Errorf("POST %s: %w", endpoint.Path, err)
	}
	defer response.Body.Close()
	if target != nil && response.StatusCode >= 200 && response.StatusCode < 300 {
		decoder := json.NewDecoder(io.LimitReader(response.Body, maxResponseBytes))
		if err := decoder.Decode(target); err != nil {
			return 0, fmt.Errorf("decode %s response: %w", endpoint.Path, err)
		}
	}
	return response.StatusCode, nil
}

func validateBaseURL(raw string) (*url.URL, error) {
	parsed, err := url.Parse(strings.TrimRight(strings.TrimSpace(raw), "/"))
	if err != nil {
		return nil, fmt.Errorf("parse API base URL: %w", err)
	}
	if parsed.Host == "" || (parsed.Scheme != "https" && parsed.Scheme != "http") {
		return nil, errors.New("API base URL must use http or https")
	}
	if parsed.Scheme == "http" && !isLoopbackHost(parsed.Hostname()) {
		return nil, errors.New("unencrypted HTTP enrollment is allowed only for localhost")
	}
	return parsed, nil
}

func defaultGatewayURL(baseURL *url.URL) string {
	copyOfURL := *baseURL
	if copyOfURL.Scheme == "https" {
		copyOfURL.Scheme = "wss"
	} else {
		copyOfURL.Scheme = "ws"
	}
	copyOfURL.Path = "/api/connectors/ws"
	copyOfURL.RawQuery = ""
	copyOfURL.Fragment = ""
	return copyOfURL.String()
}

func resolveGatewayURL(baseURL *url.URL, raw string) (string, error) {
	reference, err := url.Parse(raw)
	if err != nil {
		return "", fmt.Errorf("parse ws_url: %w", err)
	}
	if reference.Scheme == "ws" || reference.Scheme == "wss" {
		return reference.String(), nil
	}
	resolved := baseURL.ResolveReference(reference)
	if resolved.Scheme == "https" {
		resolved.Scheme = "wss"
	} else {
		resolved.Scheme = "ws"
	}
	return resolved.String(), nil
}

func isLoopbackHost(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	address := net.ParseIP(host)
	return address != nil && address.IsLoopback()
}
