import { BarChart3, Bell, ChevronDown, CircleHelp, FileSpreadsheet, FileText, Globe, History, LayoutDashboard, Link2, List, LogOut, Mail, Menu, Plug, Plus, Search, Settings, ShieldCheck, Sparkles, Target, Users, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import type { User } from '../types/auth'

type Props = { user: User; onLogout: () => Promise<void>; children?: ReactNode }
type NavItem = { label: string; icon: typeof LayoutDashboard; permission?: string; path: string }

const navGroups: { label: string; items: NavItem[] }[] = [
  { label: 'Dashboard', items: [{ label: 'Dashboard', icon: LayoutDashboard, permission: 'analytics.read', path: '/dashboard' }] },
  { label: 'Contacts', items: [{ label: 'All Contacts', icon: Users, permission: 'contacts.read', path: '/contacts' }, { label: 'Lists', icon: List, permission: 'contacts.read', path: '/contact-lists' }, { label: 'Segments', icon: Target, permission: 'contacts.read', path: '/segments' }, { label: 'Import', icon: FileSpreadsheet, permission: 'contacts.create', path: '/contacts/import' }] },
  { label: 'Campaigns', items: [{ label: 'All Campaigns', icon: Mail, permission: 'campaigns.read', path: '/campaigns' }, { label: 'Create Campaign', icon: Plus, permission: 'campaigns.create', path: '/campaigns/new' }, { label: 'Templates', icon: FileText, permission: 'templates.read', path: '/templates' }] },
  { label: 'AI Studio', items: [{ label: 'Message Studio', icon: Sparkles, permission: 'templates.create', path: '/ai/message-studio' }, { label: 'AI History', icon: History, permission: 'templates.read', path: '/inbox' }] },
  { label: 'Senders', items: [{ label: 'Email Accounts', icon: ShieldCheck, permission: 'senders.read', path: '/senders' }, { label: 'Domains', icon: Globe, permission: 'senders.read', path: '/domains' }, { label: 'Sender Health', icon: ShieldCheck, permission: 'senders.read', path: '/senders' }] },
  { label: 'Reports', items: [{ label: 'Overview', icon: BarChart3, permission: 'analytics.read', path: '/reports' }, { label: 'Campaign Reports', icon: BarChart3, permission: 'analytics.read', path: '/reports' }, { label: 'Engagement', icon: BarChart3, permission: 'analytics.read', path: '/reports' }] },
  { label: 'Workspace Management', items: [{ label: 'Integrations', icon: Plug, permission: 'integrations.read', path: '/integrations' }, { label: 'Email Providers', icon: Link2, permission: 'integrations.read', path: '/settings/email-providers' }, { label: 'Workspace Mailboxes', icon: Mail, permission: 'integrations.read', path: '/settings/email-providers/mailboxes' }, { label: 'Senders', icon: ShieldCheck, permission: 'integrations.read', path: '/senders/workspace' }, { label: 'Sending Policies', icon: ShieldCheck, permission: 'campaigns.read', path: '/policies' }, { label: 'Workspace Admin', icon: Settings, permission: 'settings.manage', path: '/admin' }, { label: 'Audit Log', icon: FileText, permission: 'audit.read', path: '/admin' }, { label: 'Platform Owner', icon: ShieldCheck, permission: 'platform.admin', path: '/platform-admin' }] },
]

const platformNavGroups: { label: string; items: NavItem[] }[] = [
  { label: 'Platform', items: [{ label: 'Platform Owner', icon: ShieldCheck, permission: 'platform.admin', path: '/platform-admin' }] },
  { label: 'Workspace Management', items: [{ label: 'Workspace Admin', icon: Settings, permission: 'platform.admin', path: '/admin' }, { label: 'Audit Log', icon: FileText, permission: 'platform.admin', path: '/admin' }] },
]

export default function Dashboard({ user, onLogout, children }: Props) {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [systemReady, setSystemReady] = useState<boolean | null>(null)
  const location = useLocation()
  useEffect(() => { let active = true; fetch('/health/ready').then((response) => { if (active) setSystemReady(response.ok) }).catch(() => { if (active) setSystemReady(false) }); return () => { active = false } }, [])
  const isPlatformOwner = user.roles.some((role) => role.trim().toUpperCase().replace(' ', '_') === 'SUPER_ADMIN')
  const hasPermission = (permission?: string) => permission === 'platform.admin' ? isPlatformOwner : !permission || user.roles.some((role) => ['Admin', 'Super Admin', 'Owner'].includes(role)) || permission === 'analytics.read'
  const visibleNavGroups = isPlatformOwner ? platformNavGroups : navGroups
  return <div className="app-shell">
    <aside className={`sidebar ${sidebarOpen ? 'sidebar-open' : ''}`}>
      <div className="sidebar-top"><Link className="brand-lockup" to="/dashboard"><span className="brand-mark">+</span><span>CR<span className="brand-accent">+</span>CRM</span></Link><button className="icon-button mobile-close" onClick={() => setSidebarOpen(false)} aria-label="Close menu"><X size={18} /></button></div>
      <div className="workspace-switcher"><span className="workspace-avatar">{user.display_name.charAt(0).toUpperCase()}</span><span><small>WORKSPACE</small><strong>Current workspace</strong></span><ChevronDown size={15} /></div>
      <nav>{visibleNavGroups.map((group) => <div className="nav-group" key={group.label}><p className="nav-label">{group.label}</p>{group.items.map(({ label, icon: Icon, permission, path }) => hasPermission(permission) && <Link className={`nav-item ${location.pathname === path ? 'active' : ''}`} key={`${group.label}-${label}`} to={path}><Icon size={16} /><span>{label}</span></Link>)}</div>)}</nav>
      <div className="sidebar-bottom"><Link className="nav-item" to="/admin"><Settings size={17} /><span>Workspace Management</span></Link><div className="sidebar-rule" /><button className="user-chip"><span className="user-avatar">{user.display_name.charAt(0).toUpperCase()}</span><span><strong>{user.display_name}</strong><small>{user.roles[0] ?? 'Member'}</small></span><ChevronDown size={15} /></button></div>
    </aside>
    {sidebarOpen && <button className="scrim" onClick={() => setSidebarOpen(false)} aria-label="Close navigation" />}
    <section className="main-area"><header className="topbar"><button className="icon-button menu-button" onClick={() => setSidebarOpen(true)} aria-label="Open menu"><Menu size={19} /></button><label className="global-search"><Search size={16} /><span className="sr-only">Search workspace</span><input placeholder="Search contacts, campaigns, templates..." /></label><div className="topbar-meta"><button className="icon-button" aria-label="Help"><CircleHelp size={18} /></button><button className="icon-button notification-button" aria-label="Notifications"><Bell size={18} /><i /></button><button className="top-user"><span className="user-avatar">{user.display_name.charAt(0).toUpperCase()}</span><span>{user.display_name}</span><small>{user.roles[0] ?? 'Member'}</small><ChevronDown size={14} /></button><button className="icon-button" onClick={() => void onLogout()} aria-label="Sign out"><LogOut size={18} /></button></div></header>{children ?? <main className="dashboard-content"><div className="page-heading"><div><p className="eyebrow">SYSTEM STATUS</p><h1>Good morning, {user.display_name.split(' ')[0]}.</h1><p className="muted">Your workspace is ready when you are.</p></div><div className={`status-pill ${systemReady === false ? 'status-warning' : ''}`}><span />{systemReady === null ? 'Checking systems...' : systemReady ? 'All systems operational' : 'System attention needed'}</div></div></main>}</section>
  </div>
}
