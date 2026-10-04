import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { authApi } from './api/client'
import type { User } from './types/auth'
import SignIn from './pages/SignIn'
import SignUp from './pages/SignUp'
import Dashboard from './pages/Dashboard'
import Contacts from './pages/Contacts'
import SuppressionCenter from './pages/SuppressionCenter'
import Duplicates from './pages/Duplicates'
import ListsSegments from './pages/ListsSegments'
import ContactLists from './pages/ContactLists'
import ContactTags from './pages/ContactTags'
import Segments from './pages/Segments'
import CustomFields from './pages/CustomFields'
import ContactForm from './pages/ContactForm'
import ContactDetails from './pages/ContactDetails'
import ContactImport from './pages/ContactImport'
import Senders from './pages/Senders'
import WorkspaceSenders from './pages/WorkspaceSenders'
import SenderDetail from './pages/SenderDetail'
import Integrations from './pages/Integrations'
import EmailProviders from './pages/EmailProviders'
import WorkspaceMailboxes from './pages/WorkspaceMailboxes'
import GoogleIntegration from './pages/GoogleIntegration'
import MicrosoftIntegration from './pages/MicrosoftIntegration'
import SenderHealth from './pages/SenderHealth'
import Domains from './pages/Domains'
import DomainDetail from './pages/DomainDetail'
import Templates from './pages/Templates'
import MessageStudio from './pages/MessageStudio'
import Campaigns from './pages/Campaigns'
import ComplianceCenter from './pages/ComplianceCenter'
import PolicyAcceptance from './pages/PolicyAcceptance'
import Conversations from './pages/Conversations'
import AnalyticsDashboard from './pages/AnalyticsDashboard'
import Inbox from './pages/Inbox'
import Reports from './pages/Reports'
import Usage from './pages/Usage'
import OpsCenter from './pages/OpsCenter'
import AdminWorkspace from './pages/AdminWorkspace'
import PlatformAdmin from './pages/PlatformAdmin'

const TOKEN_STORAGE_KEY = 'crcrm_access_token'
const REFRESH_LEEWAY_MS = 60_000

function tokenExpiryMs(token: string): number | null {
  const payload = token.split('.')[1]
  if (!payload) return null
  try {
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized.padEnd(normalized.length + ((4 - (normalized.length % 4)) % 4), '=')
    const claims = JSON.parse(atob(padded)) as { exp?: number }
    return typeof claims.exp === 'number' ? claims.exp * 1000 : null
  } catch {
    return null
  }
}

export default function App() {
  const location = useLocation()
  const [authReady, setAuthReady] = useState(false)
  const [accessToken, setAccessToken] = useState<string | null>(null)
  const [user, setUser] = useState<User | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const storedToken = localStorage.getItem(TOKEN_STORAGE_KEY)
    if (!storedToken) {
      setAuthReady(true)
      return
    }
    let active = true
    const adopt = (token: string, currentUser: User) => {
      if (!active) return
      localStorage.setItem(TOKEN_STORAGE_KEY, token)
      setAccessToken(token)
      setUser(currentUser)
    }
    void authApi.me(storedToken)
      .then((currentUser) => {
        adopt(storedToken, currentUser)
      })
      .catch(() => authApi.refresh()
        .then((issued) => authApi.me(issued.access_token).then((currentUser) => adopt(issued.access_token, currentUser)))
        .catch(() => {
          if (active) {
            localStorage.removeItem(TOKEN_STORAGE_KEY)
            setError('Your session expired. Please sign in again.')
          }
        }))
      .finally(() => {
        if (active) setAuthReady(true)
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!accessToken) return
    const expiresAt = tokenExpiryMs(accessToken)
    if (expiresAt === null) return
    let active = true
    const timer = window.setTimeout(() => {
      void authApi.refresh()
        .then((issued) => {
          if (!active) return
          localStorage.setItem(TOKEN_STORAGE_KEY, issued.access_token)
          setAccessToken(issued.access_token)
        })
        .catch(() => {
          if (!active) return
          localStorage.removeItem(TOKEN_STORAGE_KEY)
          setAccessToken(null)
          setUser(null)
        })
    }, Math.max(expiresAt - Date.now() - REFRESH_LEEWAY_MS, 0))
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [accessToken])

  if (!authReady) return <div className="loading-screen">Loading workspace</div>

  if (!accessToken || !user) {
    return (
      <Routes>
        <Route path="/sign-in" element={<SignIn />} />
        <Route path="/sign-up" element={<SignUp />} />
        <Route path="/login" element={<Navigate to="/sign-in" replace />} />
        <Route path="/signup" element={<Navigate to="/sign-up" replace />} />
        <Route path="*" element={<Navigate to={`/sign-in?next=${encodeURIComponent(location.pathname)}`} replace />} />
      </Routes>
    )
  }

  if (error) {
    return (
      <div className="loading-screen">
        <div className="table-state error-state" role="alert">
          Unable to load your workspace: {error}
        </div>
      </div>
    )
  }

  const handleLogout = async () => {
    await authApi.logout().catch(() => undefined)
    localStorage.removeItem(TOKEN_STORAGE_KEY)
    setAccessToken(null)
    setUser(null)
  }
  const wrap = (page: ReactNode) => (
    <Dashboard user={user} onLogout={handleLogout}>{page}</Dashboard>
  )

  return (
    <Routes>
      <Route path="/login" element={<Navigate to="/dashboard" replace />} />
      <Route path="/signup" element={<Navigate to="/dashboard" replace />} />
      <Route path="/sign-in" element={<Navigate to="/dashboard" replace />} />
      <Route path="/sign-up" element={<Navigate to="/dashboard" replace />} />
      <Route path="/dashboard" element={wrap(<AnalyticsDashboard accessToken={accessToken} user={user} />)} />
      <Route path="/contacts" element={wrap(<Contacts accessToken={accessToken} />)} />
      <Route path="/suppression" element={wrap(<SuppressionCenter accessToken={accessToken} />)} />
      <Route path="/duplicates" element={wrap(<Duplicates accessToken={accessToken} />)} />
      <Route path="/lists-segments" element={wrap(<ListsSegments accessToken={accessToken} />)} />
      <Route path="/contact-lists" element={wrap(<ContactLists accessToken={accessToken} />)} />
      <Route path="/tags" element={wrap(<ContactTags accessToken={accessToken} />)} />
      <Route path="/segments" element={wrap(<Segments accessToken={accessToken} />)} />
      <Route path="/custom-fields" element={wrap(<CustomFields accessToken={accessToken} />)} />
      <Route path="/contacts/new" element={wrap(<ContactForm accessToken={accessToken} />)} />
      <Route path="/contacts/import" element={wrap(<ContactImport accessToken={accessToken} />)} />
      <Route path="/contacts/:id/edit" element={wrap(<ContactForm accessToken={accessToken} />)} />
      <Route path="/contacts/:id" element={wrap(<ContactDetails accessToken={accessToken} />)} />
      <Route path="/senders" element={wrap(<Senders accessToken={accessToken} />)} />
      <Route path="/senders/workspace/:id" element={wrap(<SenderDetail accessToken={accessToken} />)} />
      <Route path="/senders/workspace" element={wrap(<WorkspaceSenders accessToken={accessToken} />)} />
      <Route path="/integrations" element={wrap(<Integrations accessToken={accessToken} />)} />
      <Route path="/settings/email-providers" element={wrap(<EmailProviders accessToken={accessToken} />)} />
      <Route path="/settings/email-providers/mailboxes" element={wrap(<WorkspaceMailboxes accessToken={accessToken} />)} />
      <Route path="/integrations/google" element={wrap(<GoogleIntegration accessToken={accessToken} />)} />
      <Route path="/integrations/microsoft" element={wrap(<MicrosoftIntegration accessToken={accessToken} />)} />
      <Route path="/senders/:id/health" element={wrap(<SenderHealth accessToken={accessToken} />)} />
      <Route path="/domains" element={wrap(<Domains accessToken={accessToken} />)} />
      <Route path="/domains/:id" element={wrap(<DomainDetail accessToken={accessToken} />)} />
      <Route path="/templates" element={wrap(<Templates accessToken={accessToken} />)} />
      <Route path="/templates/:id" element={wrap(<Templates accessToken={accessToken} />)} />
      <Route path="/ai/message-studio" element={wrap(<MessageStudio accessToken={accessToken} />)} />
      <Route path="/campaigns" element={wrap(<Campaigns accessToken={accessToken} />)} />
      <Route path="/campaigns/:id" element={wrap(<Campaigns accessToken={accessToken} />)} />
      <Route path="/campaigns/:id/review" element={wrap(<Campaigns accessToken={accessToken} />)} />
      <Route path="/compliance" element={wrap(<ComplianceCenter accessToken={accessToken} />)} />
      <Route path="/policies" element={wrap(<PolicyAcceptance accessToken={accessToken} />)} />
      <Route path="/conversations" element={wrap(<Conversations accessToken={accessToken} />)} />
      <Route path="/inbox" element={wrap(<Inbox accessToken={accessToken} />)} />
      <Route path="/reports" element={wrap(<Reports accessToken={accessToken} />)} />
      <Route path="/usage" element={wrap(<Usage accessToken={accessToken} />)} />
      <Route path="/ops" element={wrap(<OpsCenter accessToken={accessToken} />)} />
      <Route path="/admin" element={wrap(<AdminWorkspace accessToken={accessToken} currentUserId={user.id} />)} />
      <Route path="/platform-admin" element={wrap(<PlatformAdmin accessToken={accessToken} />)} />
      <Route path="*" element={<Navigate to="/dashboard" replace />} />
    </Routes>
  )
}