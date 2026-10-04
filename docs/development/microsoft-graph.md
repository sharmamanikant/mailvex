# Microsoft 365 and Microsoft Graph Integration

## Microsoft Entra Configuration

1. Register a Web application in Microsoft Entra admin center.
2. Add the exact redirect URI from `MICROSOFT_REDIRECT_URI`, locally:
   `http://localhost:8000/api/v1/senders/microsoft/callback`.
3. Add delegated Microsoft Graph permissions: `User.Read`, `Mail.Send`, and `offline_access`.
4. Grant tenant admin consent when required by the organization policy.
5. Set `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT_ID`, and `MICROSOFT_REDIRECT_URI` only in the server environment.

## Flow

`GET /api/v1/senders/microsoft/connect` creates a signed, tenant/user-bound, expiring state and returns the Entra authorization URL. The callback exchanges the code server-side, calls Microsoft Graph `/me` to validate the mailbox address, and stores the token response encrypted. Provider tokens and client secrets never reach frontend JavaScript.

## Provider Behavior

`MicrosoftGraphProvider` implements the shared `EmailProviderInterface`. It supports profile retrieval, mailbox validation, HTML mail, draft creation, message lookup, and conversation lookup. Refresh-token support is handled server-side when provider credentials are loaded. Graph `429` and `503` responses produce a safe error with `Retry-After`; a scheduler must defer retry according to policy. No quota bypass, account rotation, or immediate retry loop is used.

Graph acceptance is not a guarantee of delivery or inbox placement. Configure sender/domain compliance independently.
