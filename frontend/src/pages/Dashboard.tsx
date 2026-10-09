import { Activity, BarChart3, Bell, ChevronDown, CircleHelp, FileSpreadsheet, FileText, Globe, LayoutDashboard, Link2, List, LogOut, Mail, Menu, Plug, Plus, Search, Settings, ShieldCheck, Sparkles, Target, Users, X, Inbox, Tags, UserRoundSearch, Ban, SlidersHorizontal, ScrollText, CreditCard, MessagesSquare } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import type { User } from '../types/auth'

type Props = { user: User; onLogout: () => Promise<void>; children?: ReactNode }
type NavItem = { label: string; icon: typeof LayoutDashboard; permission?: string; path: string }

const navGroups: { label: string; items: NavItem[] }[] = [
  { label: 'Dashboard', items: [{ label: 'Dashboard', icon: LayoutDashboard, permission: 'analytics.read', path: '/dashboard' }] },
  { label: 'Contacts', items: [{ label: 'All Contacts', icon: Users, path: '/contacts' }, { label: 'Lists', icon: List, path: '/contact-lists' }, { label: 'Segments', icon: Target, path: '/segments' }, { label: 'Tags', icon: Tags, path: '/tags' }, { label: 'Custom Fields', icon: SlidersHorizontal, path: '/custom-fields' }, { label: 'Import', icon: FileSpreadsheet, path: '/contacts/import' }, { label: 'Duplicates', icon: UserRoundSearch, path: '/duplicates' }, { label: 'Suppression', icon: Ban, path: '/suppression' }] },
  { label: 'Campaigns', items: [{ label: 'All Campaigns', icon: Mail, path: '/campaigns' }, { label: 'Create Campaign', icon: Plus, path: '/campaigns/new' }, { label: 'Templates', icon: FileText, path: '/templates' }] },
  { label: 'AI Studio', items: [{ label: 'Message Studio', icon: Sparkles, path: '/ai/message-studio' }] },
  { label: 'Inbox', items: [{ label: 'Unified Inbox', icon: Inbox, path: '/inbox' }, { label: 'Conversations', icon: MessagesSquare, path: '/conversations' }] },
  { label: 'Senders', items: [{ label: 'Email Accounts', icon: ShieldCheck, path: '/senders' }, { label: 'Domains', icon: Globe, path: '/domains' }] },
  { label: 'Reports', items: [{ label: 'Reports', icon: BarChart3, path: '/reports' }, { label: 'Usage & Billing', icon: CreditCard, path: '/usage' }] },
  { label: 'Workspace Management', items: [{ label: 'Integrations', icon: Plug, path: '/integrations' }, { label: 'Email Providers', icon: Link2, path: '/settings/email-providers' }, { label: 'Workspace Mailboxes', icon: Mail, path: '/settings/email-providers/mailboxes' }, { label: 'Workspace Senders', icon: ShieldCheck, path: '/senders/workspace' }, { label: 'Sending Policies', icon: ShieldCheck, path: '/policies' }, { label: 'Compliance Center', icon: ScrollText, path: '/compliance' }, { label: 'Workspace Admin', icon: Settings, permission: 'settings.manage', path: '/admin' }, { label: 'Operations', icon: Activity, permission: 'settings.manage', path: '/ops' }, { label: 'Platform Owner', icon: ShieldCheck, permission: 'platform.admin', path: '/platform-admin' }] },
]

export default function Dashboard({ user, onLogout, children }: Props) {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [systemReady, setSystemReady] = useState<boolean | null>(null)
  const location = useLocation()
  useEffect(() => { let active = true; fetch('/health/ready').then((response) => { if (active) setSystemReady(response.ok) }).catch(() => { if (active) setSystemReady(false) }); return () => { active = false } }, [])
const normalizedRoles = new Set(user.roles.map((role) => role.trim().toUpperCase().replaceAll(' ', '_')))
  const isPlatformOwner = normalizedRoles.has('SUPER_ADMIN')
  // Mirrors the backend guards. `/auth/me` also returns a "*" wildcard for
  // privileged roles, but honouring them locally keeps navigation correct
  // before (or without) that permission list, instead of hiding admin tooling.
  const isPrivileged = ['ADMIN', 'SUPER_ADMIN', 'OWNER'].some((role) => normalizedRoles.has(role))
  const granted = new Set(user.permissions ?? [])
  const hasPermission = (permission?: string) => {
    if (!permission) return true
    if (permission === 'platform.admin') return isPlatformOwner
    return isPrivileged || granted.has('*') || granted.has(permission)
  }
  const visibleNavGroups = navGroups
    .map((group) => ({ ...group, items: group.items.filter(({ permission }) => hasPermission(permission)) }))
    .filter((group) => group.items.length)
  return <div className="app-shell">
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`}>
      <div className="sidebar-top"><Link className="brand-lockup" to="/dashboard"><span className="brand-mark">+</span><span>CR<span className="brand-accent">+</span>CRM</span></Link><button className="icon-button mobile-close" onClick={() => setSidebarOpen(false)} aria-label="Close menu"><X size={18} /></button></div>
      <div className="workspace-switcher"><span className="workspace-avatar">{user.display_name.charAt(0).toUpperCase()}</span><span><small>WORKSPACE</small><strong>Current workspace</strong></span><ChevronDown size={15} /></div>
      <nav>{visibleNavGroups.map((group) => { const items = group.items.filter(({ permission }) => hasPermission(permission)); return items.length ? <div className="nav-group" key={group.label}><p className="nav-label">{group.label}</p>{items.map(({ label, icon: Icon, path }) => <Link className={`nav-item ${location.pathname === path || (path !== '/dashboard' && path !== '/campaigns' && location.pathname.startsWith(`${path}/`)) ? 'active' : ''}`} key={`${group.label}-${label}`} to={path} onClick={() => setSidebarOpen(false)}><Icon size={16} /><span>{label}</span></Link>)}</div> : null })}</nav>
      <div className="sidebar-bottom"><Link className="nav-item" to="/admin"><Settings size={17} /><span>Workspace Management</span></Link><div className="sidebar-rule" /><button className="user-chip"><span className="user-avatar">{user.display_name.charAt(0).toUpperCase()}</span><span><strong>{user.display_name}</strong><small>{user.roles[0] ?? 'Member'}</small></span><ChevronDown size={15} /></button></div>
    </aside>
    {sidebarOpen && <button className="scrim" onClick={() => setSidebarOpen(false)} aria-label="Close navigation" />}
    <section className="main-area"><header className="topbar"><button className="icon-button menu-button" onClick={() => setSidebarOpen(true)} aria-label="Open menu"><Menu size={19} /></button><label className="global-search"><Search size={16} /><span className="sr-only">Search workspace</span><input placeholder="Search contacts, campaigns, templates..." /></label><div className="topbar-meta"><button className="icon-button" aria-label="Help"><CircleHelp size={18} /></button><button className="icon-button notification-button" aria-label="Notifications"><Bell size={18} /><i /></button><button className="top-user"><span className="user-avatar">{user.display_name.charAt(0).toUpperCase()}</span><span>{user.display_name}</span><small>{user.roles[0] ?? 'Member'}</small><ChevronDown size={14} /></button><button className="icon-button" onClick={() => void onLogout()} aria-label="Sign out"><LogOut size={18} /></button></div></header>{children ?? <main className="dashboard-content"><div className="page-heading"><div><p className="eyebrow">SYSTEM STATUS</p><h1>Good morning, {user.display_name.split(' ')[0]}.</h1><p className="muted">Your workspace is ready when you are.</p></div><div className={`status-pill ${systemReady === false ? 'status-warning' : ''}`}><span />{systemReady === null ? 'Checking systems...' : systemReady ? 'All systems operational' : 'System attention needed'}</div></div></main>}</section>
  </div>
}
