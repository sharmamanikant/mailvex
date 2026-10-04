export type EmailStatus = 'VALID' | 'INVALID' | 'UNKNOWN'
export type EmailType = 'FREE_MAILBOX' | 'BUSINESS' | 'ROLE' | 'DISPOSABLE' | 'UNKNOWN'
export type DomainStatus = 'EXISTS' | 'NXDOMAIN' | 'UNRESOLVED' | 'UNKNOWN'
export type MxStatus = 'VALID' | 'MISSING' | 'ERROR' | 'UNKNOWN'
export type SmtpStatus = 'ACCEPTED' | 'REJECTED' | 'TEMPORARY_FAILURE' | 'TIMEOUT' | 'CATCH_ALL' | 'UNKNOWN' | 'NOT_PROBED'
export type PhoneStatus = 'VALID' | 'INVALID' | 'NOT_PROVIDED' | 'UNKNOWN'
export type DuplicateStatus = 'UNIQUE' | 'POSSIBLE_DUPLICATE' | 'DUPLICATE' | 'UNKNOWN'
export type VerificationStatus = 'VERIFIED' | 'LIKELY_VALID' | 'NEEDS_REVIEW' | 'RISKY' | 'INVALID' | 'DUPLICATE' | 'UNKNOWN' | 'NOT_VERIFIED'
export type RiskLevel = 'LOW' | 'MEDIUM' | 'HIGH' | 'UNKNOWN'

export type Contact = {
  id: string
  tenant_id: string
  first_name: string | null
  last_name: string | null
  email: string
  phone: string | null
  company: string | null
  designation: string | null
  location: string | null
  website: string | null
  industry: string | null
  source: string | null
  source_reference: string | null
  status: string
  validation_status: string
  suppression_status: string
  unsubscribe_status: string
  custom_fields: Record<string, string>
  tags: string[]
  list_ids: string[]
  email_status: EmailStatus
  email_type: EmailType
  email_provider: string
  domain_status: DomainStatus
  mx_status: MxStatus
  smtp_status: SmtpStatus
  disposable: boolean
  role_account: boolean
  catch_all: boolean | null
  phone_status: PhoneStatus
  phone_type: string
  company_status: string
  duplicate_status: DuplicateStatus
  duplicate_score: number | null
  verification_score: number | null
  risk_level: RiskLevel
  verification_status: VerificationStatus
  last_verified_at: string | null
  verification_details: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export type VerificationJob = {
  id: string
  status: 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'
  scope: string
  total_count: number
  processed_count: number
  valid_count: number
  invalid_count: number
  risky_count: number
  needs_review_count: number
  duplicate_count: number
  unknown_count: number
  failed_count: number
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export type VerificationSummary = {
  by_status: Record<string, number>
  by_risk: Record<string, number>
  total: number
}

export type BulkVerifyInput = {
  contact_ids?: string[]
  probe_smtp?: boolean
  all_contacts?: boolean
  source?: string
  status?: string
  list_id?: string
}

export type ContactPage = { items: Contact[]; page: number; page_size: number; total: number }
export type ContactInput = Omit<Contact, 'id' | 'tenant_id' | 'validation_status' | 'suppression_status' | 'unsubscribe_status' | 'tags' | 'list_ids' | 'created_at' | 'updated_at' | 'email_status' | 'email_type' | 'email_provider' | 'domain_status' | 'mx_status' | 'smtp_status' | 'disposable' | 'role_account' | 'catch_all' | 'phone_status' | 'phone_type' | 'company_status' | 'duplicate_status' | 'duplicate_score' | 'verification_score' | 'risk_level' | 'verification_status' | 'last_verified_at' | 'verification_details'> & { tag_ids?: string[]; list_ids?: string[] }
export type ContactList = { id: string; tenant_id: string; name: string; description: string | null; created_at: string; updated_at: string }
export type ContactTag = { id: string; tenant_id: string; name: string; created_at: string; updated_at: string }

export type CustomFieldType = 'TEXT' | 'NUMBER' | 'BOOLEAN' | 'DATE' | 'SELECT' | 'MULTI_SELECT'
export type ContactFieldDefinition = {
  id: string
  tenant_id: string
  key: string
  label: string
  field_type: CustomFieldType
  options: string[]
  required: boolean
  created_at: string
  updated_at: string
}
export type ContactFieldDefinitionInput = { key: string; label: string; field_type: CustomFieldType; options?: string[]; required?: boolean }
export type ContactFieldDefinitionUpdate = { label?: string; field_type?: CustomFieldType; options?: string[]; required?: boolean }

export type SegmentOperator = 'eq' | 'neq' | 'contains' | 'not_contains' | 'gt' | 'gte' | 'lt' | 'lte' | 'in' | 'not_in' | 'is_empty' | 'is_not_empty'
export type SegmentCondition = { field: string; operator: SegmentOperator; value?: string | number | string[] | null }
export type ContactSegmentFilter = { match: 'all' | 'any'; conditions: SegmentCondition[] }
export type ContactSegment = { id: string; tenant_id: string; name: string; description: string | null; filters: ContactSegmentFilter; created_at: string; updated_at: string }
export type ContactSegmentInput = { name: string; description?: string | null; filters?: ContactSegmentFilter }

export type BulkAction = 'delete' | 'tag' | 'untag' | 'list' | 'unlist' | 'status'
export type BulkInput = { contact_ids: string[]; action: BulkAction; target_id?: string; status?: string }
export type BulkActionResult = { affected: number; skipped: number; failed: number }