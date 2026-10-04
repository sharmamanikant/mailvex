import { useState } from "react";
import DOMPurify from "dompurify";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  BarChart2,
  CheckCircle2,
  FileText,
  Mail,
  Pause,
  Play,
  Plus,
  Send,
  ShieldCheck,
  XCircle,
} from "lucide-react";
import { campaignsApi } from "../api/campaigns";
import { contactsApi } from "../api/contacts";
import { sendersApi } from "../api/senders";
import { templatesApi } from "../api/templates";
import type { Campaign, CampaignInput } from "../types/campaigns";
import type { ContactList } from "../types/contacts";
import type { Sender } from "../types/senders";
import type { TemplateDetail } from "../types/templates";

const initial: CampaignInput = {
  name: "",
  objective: "",
  sender_id: "",
  template_version_id: "",
  recipient_list_id: "",
  timezone: "UTC",
  timezone_policy: "UTC",
  follow_up_policy: { enabled: false },
  variable_mapping: {},
  schedule_config: { start_at: "" },
};
type TemplateContent = {
  subject_template: string;
  html_body: string;
  text_body: string | null;
  variables: string[];
};
const samples: Record<string, string> = {
  first_name: "Rajesh",
  last_name: "Kumar",
  company: "ABC Technologies",
  designation: "IT Manager",
  location: "Bengaluru",
  industry: "Technology",
  sender_name: "Manikant Sharma",
  sender_company: "DML IT Services",
  sender_phone: "+91 98765 43210",
  sender_signature: "Manikant Sharma<br>DML IT Services",
};
const resolve = (value: string, values: Record<string, string> = samples) =>
  value.replace(
    /{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}/g,
    (_, name: string) => values[name] ?? `[${name}]`,
  );

export default function Campaigns({ accessToken }: { accessToken: string }) {
  const { id } = useParams();
  const location = useLocation();
  const isReview = /\/review$/.test(location.pathname);
  const detail = useQuery({
    queryKey: ["campaign", id],
    queryFn: () => campaignsApi.get(id!, accessToken),
    enabled: Boolean(id) && id !== "new",
  });
  const list = useQuery({
    queryKey: ["campaigns"],
    queryFn: () => campaignsApi.list(accessToken),
    enabled: !id,
  });
  if (id === "new") return <CampaignEditor accessToken={accessToken} />;
  if (!id)
    return (
      <main className="campaign-page">
        <Link to="/campaigns/new" className="primary-button compact">
          <Plus size={16} /> New campaign
        </Link>
        {list.data?.map((campaign) => (
          <Link
            to={`/campaigns/${campaign.id}`}
            className="campaign-card"
            key={campaign.id}
          >
            <strong>{campaign.name}</strong>
            <span>{campaign.status}</span>
          </Link>
        ))}
      </main>
    );
  if (isReview && detail.data)
    return <CampaignReview accessToken={accessToken} detail={detail.data} />;
  return detail.data ? (
    <CampaignDetailView accessToken={accessToken} detail={detail.data} />
  ) : (
    <main className="campaign-page">
      <div className="table-state">Loading campaign...</div>
    </main>
  );
}

function CampaignEditor({ accessToken }: { accessToken: string }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [form, setForm] = useState(initial);
  const lists = useQuery({
    queryKey: ["contact-lists"],
    queryFn: () => contactsApi.lists(accessToken),
  });
  const senders = useQuery({
    queryKey: ["senders"],
    queryFn: () => sendersApi.list(accessToken),
  });
  const templates = useQuery({
    queryKey: ["templates"],
    queryFn: () => templatesApi.list(accessToken),
  });
  const save = useMutation({
    mutationFn: () =>
      campaignsApi.create(
        {
          ...form,
          template_version_id: form.template_version_id || undefined,
          recipient_list_id: form.recipient_list_id || undefined,
        },
        accessToken,
      ),
    onSuccess: (data) => {
      void client.invalidateQueries({ queryKey: ["campaigns"] });
      navigate(`/campaigns/${data.campaign.id}`);
    },
  });
  const update = (
    key: keyof CampaignInput,
    value: CampaignInput[keyof CampaignInput],
  ) => setForm((current) => ({ ...current, [key]: value }));
  return (
    <main className="campaign-page">
      <Link to="/campaigns" className="back-link">
        <ArrowLeft size={16} /> Back to campaigns
      </Link>
      <h1>Build a campaign</h1>
      <section className="campaign-section">
        <label className="form-field">
          <span>Campaign name</span>
          <input
            value={form.name}
            onChange={(e) => update("name", e.target.value)}
          />
        </label>
        <label className="form-field">
          <span>Goal & CTA</span>
          <textarea
            value={form.objective}
            onChange={(e) => update("objective", e.target.value)}
          />
        </label>
        <label className="form-field">
          <span>Description</span>
          <textarea
            value={form.description ?? ""}
            onChange={(e) => update("description", e.target.value)}
          />
        </label>
        <label className="form-field">
          <span>Recipient list</span>
          <select
            value={form.recipient_list_id}
            onChange={(e) => update("recipient_list_id", e.target.value)}
          >
            <option value="">Choose a recipient list</option>
            {lists.data?.map((item: ContactList) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label className="form-field">
          <span>Sender</span>
          <select
            value={form.sender_id}
            onChange={(e) => update("sender_id", e.target.value)}
          >
            <option value="">Choose a sender</option>
            {senders.data?.map((item: Sender) => (
              <option key={item.id} value={item.id}>
                {item.display_name || item.email}
              </option>
            ))}
          </select>
        </label>
        <label className="form-field">
          <span>Email template</span>
          <select
            value={form.template_version_id}
            onChange={(e) => update("template_version_id", e.target.value)}
          >
            <option value="">Choose a template</option>
            {templates.data?.items.map((item: TemplateDetail) => (
              <option key={item.version.id} value={item.version.id}>
                {item.template.name}
              </option>
            ))}
          </select>
        </label>
        <label className="form-field">
          <span>Start time</span>
          <input
            type="datetime-local"
            value={String(form.schedule_config?.start_at ?? "")}
            onChange={(e) =>
              update("schedule_config", {
                ...form.schedule_config,
                start_at: e.target.value,
              })
            }
            required
          />
        </label>
        <label className="form-field">
          <span>Timezone</span>
          <select
            value={form.timezone_policy}
            onChange={(e) => update("timezone_policy", e.target.value)}
          >
            <option>UTC</option>
            <option>America/New_York</option>
            <option>Asia/Kolkata</option>
          </select>
        </label>
        <button
          className="primary-button"
          disabled={save.isPending}
          onClick={() => void save.mutate()}
        >
          {save.isPending ? "Creating..." : "Create draft campaign"}
        </button>
      </section>
    </main>
  );
}

function CampaignDetailView({
  accessToken,
  detail,
}: {
  accessToken: string;
  detail: {
    campaign: Campaign;
    recipient_ids: string[];
    template_content?: TemplateContent | null;
    recipient_preview?: Record<string, string>;
  };
}) {
  const client = useQueryClient();
  const campaign = detail.campaign;
  const action = useMutation<unknown, Error, string>({
    mutationFn: (status) =>
      campaignsApi.status(campaign.id, status, accessToken),
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: ["campaign", campaign.id] }),
  });
  const sendNow = useMutation({
    mutationFn: () => campaignsApi.sendNow(campaign.id, accessToken),
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: ["campaign", campaign.id] }),
  });
  const exporting = useMutation({
    mutationFn: () => campaignsApi.analyticsExport(campaign.id, accessToken),
  });
  const progress = useQuery({
    queryKey: ["campaign-delivery-progress", campaign.id],
    queryFn: () => campaignsApi.deliveryProgress(campaign.id, accessToken),
    enabled:
      ["SCHEDULED", "RUNNING", "PAUSED", "COMPLETED"].includes(
        campaign.status,
      ),
    refetchInterval: (query) =>
      ["SCHEDULED", "RUNNING", "PAUSED"].includes(query.state.data?.status ?? "")
        ? 5000
        : false,
  });
  const analytics = useQuery({
    queryKey: ["campaign-analytics", campaign.id],
    queryFn: () => campaignsApi.analytics(campaign.id, accessToken),
    enabled: ["RUNNING", "PAUSED", "COMPLETED"].includes(campaign.status),
    refetchInterval: 15000,
  });
  const transitions: Record<
    string,
    { label: string; next: string; icon: typeof Play }[]
  > = {
    DRAFT: [{ label: "Send to review", next: "REVIEW", icon: FileText }],
    REVIEW: [],
    APPROVED: [{ label: "Schedule campaign", next: "SCHEDULED", icon: Send }],
    SCHEDULED: [
      { label: "Pause", next: "PAUSED", icon: Pause },
      { label: "Cancel", next: "CANCELLED", icon: XCircle },
    ],
    PAUSED: [{ label: "Resume", next: "SCHEDULED", icon: Play }],
  };
  const content = detail.template_content;
  return (
    <main className="campaign-page">
      <Link to="/campaigns" className="back-link">
        <ArrowLeft size={16} /> Back to campaigns
      </Link>
      <div className="campaign-detail-heading">
        <div>
          <p className="eyebrow">CAMPAIGN DETAIL</p>
          <h1>{campaign.name}</h1>
          <p className="muted">{campaign.objective}</p>
        </div>
        <span
          className={`campaign-status large ${campaign.status.toLowerCase()}`}
        >
          {campaign.status}
        </span>
      </div>
      <div className="campaign-detail-grid">
        <InfoCard
          icon={Send}
          title="Recipients"
          value={`${campaign.recipient_count} selected`}
        />
        <InfoCard icon={Mail} title="Sender" value={campaign.sender_id} />
        <InfoCard
          icon={FileText}
          title="Template"
          value={campaign.template_version_id ?? "Not selected"}
        />
        <InfoCard
          icon={ShieldCheck}
          title="Start time"
          value={String(campaign.schedule_config.start_at ?? "Not scheduled")}
        />
      </div>
      {progress.data && (
        <section className="delivery-progress">
          <div className="delivery-progress-head">
            <h2>Delivery progress</h2>
            <span className={`campaign-status small ${progress.data.status.toLowerCase()}`}>
              {progress.data.status}
            </span>
          </div>
          <DeliveryProgressBar data={progress.data} />
          <div className="delivery-progress-counts">
            {Object.entries(progress.data.counts).map(([status, count]) => (
              <span className={`delivery-count ${status.toLowerCase()}`} key={status}>
                <strong>{count}</strong> {status}
              </span>
            ))}
          </div>
        </section>
      )}
      {analytics.data && (
        <section className="campaign-analytics">
          <div className="campaign-analytics-head">
            <h2>Campaign metrics</h2>
            <button
              type="button"
              className="secondary-button compact"
              disabled={exporting.isPending}
              onClick={() => void exporting.mutate()}
            >
              {exporting.isPending ? "Exporting..." : "Export CSV"}
            </button>
          </div>
          <div className="campaign-analytics-grid">
            {Object.entries(analytics.data.metrics).map(([key, value]) => {
              const pct =
                analytics.data.percentages[key] != null
                  ? analytics.data.percentages[key].toFixed(1)
                  : "—";
              return (
                <article className="campaign-analytics-metric" key={key}>
                  <BarChart2 size={17} />
                  <small>{key}</small>
                  <strong>{value}</strong>
                  <span className="analytics-pct">{pct}%</span>
                </article>
              );
            })}
          </div>
        </section>
      )}
      {content && (
        <section className="campaign-review">
          <h2>Email preview</h2>
          <p>
            <strong>Subject:</strong> {resolve(content.subject_template)}
          </p>
          <h3>Variables</h3>
          <p>
            {content.variables.map((name) => `{{${name}}}`).join(", ") ||
              "None"}
          </p>
          <h3>How recipients will receive it</h3>
          <div className="email-preview">
            <div
              dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(resolve(content.html_body)) }}
            />
          </div>
          {content.text_body && (
            <>
              <h3>Plain-text fallback</h3>
              <pre>{resolve(content.text_body)}</pre>
            </>
          )}
        </section>
      )}
      <div className="campaign-actions">
        {campaign.status === "SCHEDULED" && (
          <button
            className="primary-button compact"
            type="button"
            disabled={action.isPending || sendNow.isPending}
            onClick={() => void sendNow.mutate()}
          >
            <Send size={15} /> {sendNow.isPending ? "Sending..." : "Send now"}
          </button>
        )}
        {transitions[campaign.status]?.map(({ label, next, icon: Icon }) => (
          <button
            className="primary-button compact"
            type="button"
            disabled={action.isPending}
            onClick={() => void action.mutate(next)}
            key={next}
          >
            <Icon size={15} /> {label}
          </button>
        ))}
        {campaign.status === "REVIEW" && (
          <Link
            to={`/campaigns/${campaign.id}/review`}
            className="primary-button compact"
          >
            <ShieldCheck size={15} /> Review & approve
          </Link>
        )}
      </div>
      {action.isError && (
        <div className="form-error">{action.error.message}</div>
      )}
      {sendNow.isError && (
        <div className="form-error">{sendNow.error.message}</div>
      )}
    </main>
  );
}
function CampaignReview({
  accessToken,
  detail,
}: {
  accessToken: string;
  detail: {
    campaign: Campaign;
    recipient_ids: string[];
    template_content?: TemplateContent | null;
    recipient_preview?: Record<string, string>;
  };
}) {
  const client = useQueryClient();
  const campaign = detail.campaign;
  const validation = useQuery({
    queryKey: ["campaign-validation", campaign.id],
    queryFn: () => campaignsApi.validate(campaign.id, accessToken),
  });
  const approve = useMutation({
    mutationFn: () => campaignsApi.approve(campaign.id, accessToken),
    onSuccess: () =>
      void client.invalidateQueries({ queryKey: ["campaign", campaign.id] }),
  });
  const blocked = (validation.data?.checks ?? []).some(
    (check) => check.outcome === "BLOCK",
  );
  return (
    <main className="campaign-page">
      <Link to={`/campaigns/${campaign.id}`} className="back-link">
        <ArrowLeft size={16} /> Back to campaign
      </Link>
      <div className="campaign-detail-heading">
        <div>
          <p className="eyebrow">CAMPAIGN REVIEW</p>
          <h1>{campaign.name}</h1>
          <p className="muted">
            {campaign.objective}
            {campaign.description ? ` — ${campaign.description}` : ""}
          </p>
        </div>
        <span
          className={`campaign-status large ${campaign.status.toLowerCase()}`}
        >
          {campaign.status}
        </span>
      </div>
      {validation.isLoading && (
        <div className="table-state">Running approval checks...</div>
      )}
      {validation.isError && (
        <div className="form-error">{String(validation.error)}</div>
      )}
      {validation.data && (
        <section
          className={`validation-panel validation-${validation.data.level.toLowerCase()}`}
        >
          <h2>
            {validation.data.level === "PASS" ? (
              <CheckCircle2 size={18} />
            ) : (
              <AlertTriangle size={18} />
            )}{" "}
            Readiness: {validation.data.level}
          </h2>
          <ul className="validation-checks">
            {validation.data.checks.map((check) => (
              <li
                className={`validation-check ${check.outcome.toLowerCase()}`}
                key={check.name}
              >
                <strong>{check.name}</strong>
                <span>{check.message}</span>
                {check.remediation && <em>{check.remediation}</em>}
              </li>
            ))}
          </ul>
        </section>
      )}
      <div className="campaign-actions">
        <button
          className="primary-button compact"
          type="button"
          disabled={approve.isPending || blocked || validation.isLoading}
          onClick={() => void approve.mutate()}
        >
          <CheckCircle2 size={15} />{" "}
          {approve.isPending ? "Approving..." : "Approve campaign"}
        </button>
      </div>
      {blocked && (
        <div className="form-error">
          This campaign has blocking issues and cannot be approved.
        </div>
      )}
      {approve.isError && (
        <div className="form-error">{approve.error.message}</div>
      )}
    </main>
  );
}
function InfoCard({
  icon: Icon,
  title,
  value,
}: {
  icon: typeof Send;
  title: string;
  value: string;
}) {
  return (
    <article className="campaign-info-card">
      <Icon size={17} />
      <small>{title}</small>
      <strong>{value}</strong>
    </article>
  );
}
function DeliveryProgressBar({
  data,
}: {
  data: {
    status: string;
    counts: Record<string, number>;
    total: number;
  };
}) {
  const c = data.counts;
  const total = data.total || 0;
  const sent = (c["SENT"] ?? 0) + (c["DELIVERED"] ?? 0);
  const failed = (c["FAILED"] ?? 0) + (c["BLOCKED"] ?? 0);
  const cancelled = c["CANCELLED"] ?? 0;
  const terminal = sent + failed + cancelled;
  const pct = total > 0 ? Math.round((terminal / total) * 100) : 0;
  const sentPct = total > 0 ? Math.round((sent / total) * 100) : 0;
  return (
    <div className="delivery-progress-body">
      <div className="delivery-progress-track">
        <div className="delivery-progress-fill" style={{ width: `${pct}%` }} />
        <div
          className="delivery-progress-sent"
          style={{ width: `${sentPct}%` }}
        />
      </div>
      <div className="delivery-progress-meta">
        <span>
          {sent} sent · {failed} failed · {cancelled} cancelled
        </span>
        <strong>
          {pct}% complete
        </strong>
      </div>
    </div>
  );
}
