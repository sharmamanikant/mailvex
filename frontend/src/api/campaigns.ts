import type {
  Campaign,
  CampaignDetail,
  CampaignInput,
} from "../types/campaigns";

async function request<T>(
  path: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: "include",
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: unknown;
    } | null;
    const detail = payload?.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : detail
          ? JSON.stringify(detail)
          : "Campaign request failed",
    );
  }
  return response.json() as Promise<T>;
}
export const campaignsApi = {
  list: (token: string) => request<Campaign[]>("/campaigns", token),
  get: (id: string, token: string) =>
    request<CampaignDetail>(`/campaigns/${id}`, token),
  create: (input: CampaignInput, token: string) =>
    request<CampaignDetail>("/campaigns", token, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  update: (id: string, input: Partial<CampaignInput>, token: string) =>
    request<CampaignDetail>(`/campaigns/${id}`, token, {
      method: "PATCH",
      body: JSON.stringify(input),
    }),
  status: (id: string, status: string, token: string): Promise<unknown> =>
    status === "SCHEDULED"
      ? campaignsApi.schedule(id, token)
      : request<CampaignDetail>(`/campaigns/${id}/status`, token, {
          method: "POST",
          body: JSON.stringify({ status }),
        }),
  schedule: (id: string, token: string) =>
    request<{ campaign_id: string; status: string; scheduled_count: number }>(
      `/campaigns/${id}/schedule`,
      token,
      { method: "POST" },
    ),
  sendNow: (id: string, token: string) =>
    request<{ campaign_id: string; status: string; queued_count: number }>(
      `/campaigns/${id}/send-now`,
      token,
      { method: "POST" },
    ),
  deliveryProgress: (id: string, token: string) =>
    request<{
      campaign_id: string;
      status: string;
      counts: Record<string, number>;
      total: number;
    }>(`/campaigns/${id}/delivery-progress`, token),
  analytics: (id: string, token: string) =>
    request<{
      campaign_id: string;
      campaign_name: string;
      status: string;
      metrics: Record<string, number>;
      percentages: Record<string, number>;
    }>(`/campaigns/${id}/analytics`, token),
  analyticsRecipients: (
    id: string,
    token: string,
    page = 1,
    pageSize = 25,
  ) =>
    request<{
      campaign_id: string;
      count: number;
      total: number;
      page: number;
      page_size: number;
      items: {
        name: string;
        email: string;
        status: string;
        status_code: string;
        timestamp: string | null;
        reason: string | null;
      }[];
    }>(
      `/campaigns/${id}/analytics/recipients?page=${page}&page_size=${pageSize}`,
      token,
    ),
  analyticsExport: (id: string, token: string) =>
    (async () => {
      const response = await fetch(
        `/api/v1/campaigns/${id}/analytics/export`,
        {
          credentials: "include",
          headers: { Authorization: `Bearer ${token}` },
        },
      );
      if (!response.ok) throw new Error("Export failed");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `campaign-${id}-results.csv`;
      anchor.click();
      URL.revokeObjectURL(url);
    })(),
  duplicate: (id: string, name: string, token: string) =>
    request<CampaignDetail>(
      `/campaigns/${id}/duplicate?name=${encodeURIComponent(name)}`,
      token,
      { method: "POST" },
    ),
  validate: (id: string, token: string) =>
    request<import("../types/campaigns").CampaignValidation>(
      `/campaigns/${id}/validate`,
      token,
      { method: "POST" },
    ),
  approve: (id: string, token: string) =>
    request<CampaignDetail>(`/campaigns/${id}/approve`, token, {
      method: "POST",
    }),
};
