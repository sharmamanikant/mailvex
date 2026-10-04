# Google Gmail Integration

## Google Cloud Setup

1. Create or select a Google Cloud project.
2. Enable the Gmail API.
3. Configure the OAuth consent screen for the intended user type and add the required test users while the application is in testing.
4. Create an OAuth 2.0 Web application client.
5. Add the exact redirect URI from `GOOGLE_REDIRECT_URI`, for local development:
   `http://localhost:8000/api/v1/senders/google/callback`.
6. Set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REDIRECT_URI` in the server environment.

## Scopes and Flow

The server requests only Gmail send and read scopes needed by the current provider contract. The browser receives an authorization URL, while the server validates signed state, exchanges the authorization code, retrieves the Gmail profile, and stores the credential payload encrypted. Access and refresh tokens are never returned to frontend code or logs.

## Provider Behavior

The Gmail adapter uses the official Gmail API client. Expired credentials are refreshed server-side. Provider `403` and `429` responses are surfaced as a safe throttling error so a future scheduler can defer retry according to policy. The integration does not bypass quotas, rotate accounts, or retry immediately.

Gmail API behavior and provider acceptance must not be interpreted as guaranteed delivery or inbox placement. Configure sender/domain compliance separately before production use.