import type { CampaignReportRow, ContactReport, DashboardReport, ReportFilter, ReportName, SenderReportRow } from '../types/reporting'

function toQuery(params: ReportFilter): string {
  const query = new URLSearchParams()
  if (params.range) query.set('range', params.range)
  if (params.start_date) query.set('start_date', params.start_date)
  if (params.end_date) query.set('end_date', params.end_date)
  const qs = query.toString()
  return qs ? `?${qs}` : ''
}

async function request<T>(path: string, token: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'include', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(init?.headers ?? {}) }, ...init })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(payload?.detail ?? 'Reporting request failed')
  }
  return response.json() as Promise<T>
}

export const reportingApi = {
  dashboard: (token: string, params: ReportFilter = {}) => request<DashboardReport>(`/reports/dashboard${toQuery(params)}`, token),
  campaigns: (token: string, params: ReportFilter = {}) => request<CampaignReportRow[]>(`/reports/campaigns${toQuery(params)}`, token),
  senders: (token: string, params: ReportFilter = {}) => request<SenderReportRow[]>(`/reports/senders${toQuery(params)}`, token),
  contacts: (token: string, params: ReportFilter = {}) => request<ContactReport>(`/reports/contacts${toQuery(params)}`, token),
  exportCsv: async (report: ReportName, token: string, params: ReportFilter = {}) => {
    const response = await fetch(`/api/v1/reports/export?report=${report}${toQuery(params)}`, { credentials: 'include', headers: { Authorization: `Bearer ${token}` } })
    if (!response.ok) {
      const payload = await response.json().catch(() => null) as { detail?: string } | null
      throw new Error(payload?.detail ?? 'CSV export failed')
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `${report}-report.csv`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  },
}