import { useEffect, useState } from "react";
import DOMPurify from "dompurify";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Archive, CheckCircle2, Eye, FileText, Plus, RotateCcw, Save } from "lucide-react";
import { templatesApi } from "../api/templates";
import { contactsApi } from "../api/contacts";
import type { TemplateInput, TemplateVersion } from "../types/templates";

const fallbackBuiltIns = [
  "first_name",
  "last_name",
  "full_name",
  "email",
  "phone",
  "company",
  "designation",
  "location",
  "website",
  "industry",
  "skills",
  "job_title",
  "experience",
  "requirement",
  "service",
  "service_area",
  "technology",
  "availability",
  "sender_name",
  "sender_email",
  "sender_company",
  "sender_designation",
  "sender_phone",
  "sender_signature",
];
const empty: TemplateInput = {
  name: "",
  description: "",
  subject_template: "Hello {{first_name}}",
  html_body:
    "<p>Hello {{first_name}},</p>\n<p>I would love to connect with {{company}}.</p>",
  text_body:
    "Hello {{first_name}},\n\nI would love to connect with {{company}}.",
  custom_variables: [],
};

type Preview = {
  subject: string;
  html_body: string;
  text_body: string;
  used_variables: string[];
  missing_variables: string[];
  warnings: string[];
};

export default function Templates({ accessToken }: { accessToken: string }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const editing = Boolean(id && id !== "new");
  const [form, setForm] = useState<TemplateInput>(empty);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [editingVersion, setEditingVersion] = useState<number | null>(null);

  const list = useQuery({
    queryKey: ["templates"],
    queryFn: () => templatesApi.list(accessToken),
    enabled: !id,
  });
  const detail = useQuery({
    queryKey: ["template", id],
    queryFn: () => templatesApi.get(id!, accessToken),
    enabled: editing,
  });
  const variables = useQuery({
    queryKey: ["template-variables"],
    queryFn: () => templatesApi.variables(accessToken),
    enabled: Boolean(accessToken),
  });
  const builtIns =
    variables.data && variables.data.recipient && variables.data.sender
      ? [...variables.data.recipient, ...variables.data.sender]
      : fallbackBuiltIns;

  useEffect(() => {
    if (detail.data) {
      const version = detail.data.version;
      setForm({
        name: detail.data.template.name,
        description: detail.data.template.description ?? "",
        subject_template: version.subject_template,
        html_body: version.html_body,
        text_body: version.text_body,
        custom_variables: version.variable_manifest.filter(
          (name) => !builtIns.find((value) => value === name),
        ),
      });
      setEditingVersion(version.version_number);
    }
  }, [detail.data]);

  const versions = useQuery({
    queryKey: ["template-versions", id],
    queryFn: () => templatesApi.versions(id!, accessToken),
    enabled: editing,
  });

  const contacts = useQuery({
    queryKey: ["contacts-small"],
    queryFn: () => contactsApi.list(new URLSearchParams({ page_size: "100" }), accessToken),
    enabled: editing,
  });

  const save = useMutation({
    mutationFn: () =>
      editing
        ? templatesApi.update(id!, form, accessToken)
        : templatesApi.create(form, accessToken),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["templates"] });
      void client.invalidateQueries({ queryKey: ["template-versions", result.template.id] });
      navigate(`/templates/${result.template.id}`);
    },
    onError: (cause) =>
      setError(
        cause instanceof Error ? cause.message : "Could not save template",
      ),
  });
  const runPreview = useMutation({
    mutationFn: () =>
      templatesApi.preview(
        id!,
        {
          recipient: { first_name: "Rajesh", company: "ABC Technologies" },
          sender: { sender_name: "Your Name", sender_company: "Your Company" },
          custom_values: {},
        },
        accessToken,
      ),
    onSuccess: setPreview,
    onError: (cause) =>
      setError(
        cause instanceof Error ? cause.message : "Could not preview template",
      ),
  });
  const runRecipientPreview = useMutation({
    mutationFn: (contactId: string) =>
      templatesApi.recipientPreview(id!, contactId, accessToken),
    onSuccess: setPreview,
    onError: (cause) =>
      setError(
        cause instanceof Error ? cause.message : "Could not preview recipient",
      ),
  });
  const setStatus = useMutation({
    mutationFn: (status: "DRAFT" | "ACTIVE" | "ARCHIVED") =>
      templatesApi.setStatus(id!, status, accessToken),
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["templates"] });
      void client.invalidateQueries({ queryKey: ["template", id] });
      setEditingVersion(result.version.version_number);
    },
    onError: (cause) =>
      setError(
        cause instanceof Error ? cause.message : "Could not change status",
      ),
  });
  const update = (key: keyof TemplateInput, value: string) => {
    setError("");
    setForm((current) => ({ ...current, [key]: value }));
  };
  const loadVersion = (version: TemplateVersion) => {
    setError("");
    setPreview(null);
    setForm({
      name: detail.data?.template.name ?? form.name,
      description: detail.data?.template.description ?? form.description ?? "",
      subject_template: version.subject_template,
      html_body: version.html_body,
      text_body: version.text_body,
      custom_variables: version.variable_manifest.filter(
        (name) => !builtIns.find((value) => value === name),
      ),
    });
    setEditingVersion(version.version_number);
  };

  if (!id)
    return (
      <main className="templates-page">
        <div className="templates-heading">
          <div>
            <p className="eyebrow">MESSAGE STUDIO</p>
            <h1>Templates</h1>
            <p className="muted">
              Reusable, human-reviewed messages for thoughtful outreach.
            </p>
          </div>
          <Link to="/templates/new" className="primary-button compact">
            <Plus size={16} /> New template
          </Link>
        </div>
        {list.isLoading ? (
          <div className="table-state">Loading templates...</div>
        ) : list.data?.items.length ? (
          <div className="template-list">
            {list.data.items.map((item) => (
              <Link
                className="template-card"
                to={`/templates/${item.template.id}`}
                key={item.template.id}
              >
                <div className="template-icon">
                  <FileText size={18} />
                </div>
                <div>
                  <strong>{item.template.name}</strong>
                  <p>{item.template.description || "No description"}</p>
                </div>
                <span
                  className={`template-status ${item.template.status.toLowerCase()}`}
                >
                  {item.template.status}
                </span>
                <span>v{item.version.version_number}</span>
              </Link>
            ))}
          </div>
        ) : (
          <div className="sender-empty">
            <div className="empty-icon">
              <FileText size={24} />
            </div>
            <h2>No templates yet</h2>
            <p>
              Create a reusable message with safe personalization variables.
            </p>
            <Link to="/templates/new" className="outline-button">
              <Plus size={15} /> Create template
            </Link>
          </div>
        )}
      </main>
    );

  const templateStatus = detail.data?.template.status ?? "DRAFT";
  const latestVersion = versions.data?.items.at(-1);
  const currentEditingLatest = editingVersion === latestVersion?.version_number;

  return (
    <main className="template-editor-page">
      <Link to="/templates" className="back-link">
        &lt;- Back to templates
      </Link>
      <div className="editor-heading">
        <div>
          <p className="eyebrow">MESSAGE STUDIO</p>
          <h1>{editing ? "Edit template" : "New template"}</h1>
          <p className="muted">
            Variables are rendered as data, never executed as code.
          </p>
        </div>
        <div className="editor-actions">
          {templateStatus === "ACTIVE" && (
            <button
              type="button"
              className="outline-button"
              disabled={setStatus.isPending}
              onClick={() => void setStatus.mutate("DRAFT")}
            >
              <RotateCcw size={15} /> Revert to draft
            </button>
          )}
          {templateStatus === "DRAFT" && (
            <button
              type="button"
              className="primary-button compact"
              disabled={setStatus.isPending}
              onClick={() => void setStatus.mutate("ACTIVE")}
            >
              <CheckCircle2 size={15} /> Activate
            </button>
          )}
          {templateStatus !== "ARCHIVED" && (
            <button
              type="button"
              className="outline-button"
              disabled={setStatus.isPending}
              onClick={() => void setStatus.mutate("ARCHIVED")}
            >
              <Archive size={15} /> Archive
            </button>
          )}
          <button
            type="button"
            className="outline-button"
            disabled={!editing || runPreview.isPending}
            onClick={() => {
              setError("");
              void runPreview.mutate();
            }}
          >
            <Eye size={15} /> Preview
          </button>
          <button
            type="submit"
            form="template-editor"
            className="primary-button compact"
            disabled={save.isPending}
          >
            <Save size={15} /> {save.isPending ? "Saving..." : "Save version"}
          </button>
        </div>
      </div>
      {editing && !currentEditingLatest && (
        <div className="form-warn">
          You are editing version {editingVersion}. Saving creates a new
          version; the historical version will not be modified.
        </div>
      )}
      <form
        id="template-editor"
        className="editor-layout"
        onSubmit={(event) => {
          event.preventDefault();
          if (
            !form.name.trim() ||
            !form.subject_template.trim() ||
            !form.html_body.trim()
          ) {
            setError("Template name, subject, and HTML body are required");
            return;
          }
          void save.mutate();
        }}
      >
        <div className="editor-main">
          <section className="editor-form">
            <label className="form-field">
              <span>Template name</span>
              <input
                value={form.name}
                onChange={(event) => update("name", event.target.value)}
                placeholder="Introductory note"
                required
              />
            </label>
            <label className="form-field">
              <span>Description</span>
              <input
                value={form.description ?? ""}
                onChange={(event) => update("description", event.target.value)}
              />
            </label>
            <label className="form-field">
              <span>Subject</span>
              <input
                value={form.subject_template}
                onChange={(event) =>
                  update("subject_template", event.target.value)
                }
                required
              />
            </label>
            <label className="form-field">
              <span>HTML body</span>
              <textarea
                className="template-textarea"
                value={form.html_body}
                onChange={(event) => update("html_body", event.target.value)}
                required
              />
            </label>
            <label className="form-field">
              <span>Plain-text fallback</span>
              <textarea
                className="template-textarea short"
                value={form.text_body ?? ""}
                onChange={(event) => update("text_body", event.target.value)}
              />
            </label>
            <div className="variable-section">
              <div className="section-heading">
                <strong>Variables</strong>
                <span>Insert a variable at the end of the HTML body.</span>
              </div>
              <div className="variable-chips">
                {builtIns.map((name) => (
                  <button
                    type="button"
                    className="variable-chip"
                    key={name}
                    onClick={() =>
                      update("html_body", `${form.html_body} {{${name}}}`)
                    }
                  >{`{{${name}}}`}</button>
                ))}
              </div>
            </div>
            {error && (
              <div className="form-error" role="alert">
                {error}
              </div>
            )}
          </section>
          {editing && versions.data && versions.data.items.length > 0 && (
            <section className="editor-form">
              <div className="variable-section">
                <div className="section-heading">
                  <strong>History</strong>
                  <span>Every save creates a new version.</span>
                </div>
                <div className="version-list">
                  {[...versions.data.items]
                    .reverse()
                    .map((version) => (
                      <button
                        type="button"
                        className="version-row"
                        key={version.id}
                        onClick={() => loadVersion(version)}
                      >
                        <strong>v{version.version_number}</strong>
                        <span>{version.status}</span>
                        <span>
                          {new Date(version.created_at).toLocaleDateString()}
                        </span>
                        <span>
                          {version.version_number === latestVersion?.version_number
                            ? "current"
                            : "view past"}
                        </span>
                      </button>
                    ))}
                </div>
              </div>
              <div className="variable-section">
                <div className="section-heading">
                  <strong>Recipient preview</strong>
                  <span>Render this template with a real contact.</span>
                </div>
                <label className="form-field">
                  <span>Contact</span>
                  <select
                    defaultValue=""
                    disabled={runRecipientPreview.isPending}
                    onChange={(event) => {
                      if (!event.target.value) return;
                      setError("");
                      runRecipientPreview.mutate(event.target.value);
                    }}
                  >
                    <option value="">Choose a contact...</option>
                    {contacts.data?.items.map((contact) => (
                      <option key={contact.id} value={contact.id}>
                        {contact.first_name || contact.email}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            </section>
          )}
        </div>
        {preview && (
          <aside className="preview-panel">
            <p className="eyebrow">PREVIEW</p>
            <h2>{preview.subject}</h2>
            <div
              dangerouslySetInnerHTML={{
                __html: DOMPurify.sanitize(preview.html_body),
              }}
            />
            <p className="muted preview-text">{preview.text_body}</p>
            {preview.missing_variables.length > 0 && (
              <div className="preview-warnings">
                {preview.warnings.map((warning) => (
                  <div className="form-warn" key={warning}>
                    {warning}
                  </div>
                ))}
              </div>
            )}
          </aside>
        )}
      </form>
    </main>
  );
}