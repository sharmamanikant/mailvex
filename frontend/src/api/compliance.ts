export type ComplianceCheck = { name: string; outcome: 'PASS' | 'WARNING' | 'BLOCK'; message: string; remediation: string | null }
export type ComplianceResult = { campaign_id: string; recipient_id: string; outcome: string; checks: ComplianceCheck[] }
export type ComplianceSummaryItem = { campaign_id: string; recipient_id: string; passed: number; warnings: number; blocked: number; outcome: string }
export type ComplianceSummary = {
  campaign_id: string
  total_recipients: number
  pass_count: number
  warning_count: number
  blocked_count: number
  detail: ComplianceSummaryItem[]
}

export async function checkCompliance(campaignId: string, recipientId: string, token: string): Promise<ComplianceResult> {
  const response = await fetch(`/api/v1/compliance/campaigns/${campaignId}/recipients/${recipientId}`, { headers: { Authorization: `Bearer ${token}` }, credentials: 'include' })
  if (!response.ok) throw new Error('Compliance check failed')
  return response.json() as Promise<ComplianceResult>
}

export async function getComplianceSummary(campaignId: string, token: string): Promise<ComplianceSummary> {
  const response = await fetch(`/api/v1/compliance/campaigns/${campaignId}/summary`, { headers: { Authorization: `Bearer ${token}` }, credentials: 'include' })
  if (!response.ok) throw new Error('Compliance summary failed')
  return response.json() as Promise<ComplianceSummary>
}
