export type TemplateInput = { name: string; description?: string | null; subject_template: string; html_body: string; text_body?: string | null; custom_variables?: string[] }
export type TemplateVersion = { id: string; tenant_id: string; template_id: string; version_number: number; subject_template: string; html_body: string; text_body: string | null; status: string; variable_manifest: string[]; created_by_id?: string | null; created_at: string; updated_at: string }
export type TemplateSummary = { id: string; tenant_id: string; name: string; description: string | null; status: string; current_version_id: string | null; created_by_id?: string | null; created_at: string; updated_at: string }
export type TemplateDetail = { template: TemplateSummary; version: TemplateVersion }
export type RenderedTemplate = { subject: string; html_body: string; text_body: string; used_variables: string[]; missing_variables: string[]; warnings: string[] }
export type TemplateVariables = { recipient: string[]; sender: string[] }
export type TemplateVersionList = { items: TemplateVersion[]; total: number }