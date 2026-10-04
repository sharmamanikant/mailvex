import { useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { authApi } from '../api/client'

export default function SignInPage() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)

  const nextPath = new URLSearchParams(window.location.search).get('next')

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setError('')
    setPending(true)
    try {
      const result = await authApi.login(email, password)
      localStorage.setItem('crcrm_access_token', result.access_token)
      window.location.assign(nextPath?.startsWith('/') ? nextPath : '/dashboard')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to sign in')
    } finally {
      setPending(false)
    }
  }

  return <AuthLayout eyebrow="WORKSPACE ACCESS" title="Make every message count." subtitle="Sign in to your outreach workspace.">
    <div className="custom-auth-card"><div className="custom-auth-heading"><h2>Sign in to your workspace</h2><p>Use your CR+CRM email and password.</p></div>{error && <div className="auth-error" role="alert">{error}</div>}<form className="custom-auth-form" onSubmit={(event) => void submit(event)}><label><span>Email address</span><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" required placeholder="you@company.com" /></label><label><span>Password</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required placeholder="Enter your password" /></label><button className="primary-button" type="submit" disabled={pending}>{pending ? 'Signing in...' : 'Sign in'}</button></form><p className="custom-auth-switch">Don&apos;t have an account? <a href="/sign-up">Create your workspace</a></p></div>
  </AuthLayout>
}

export function AuthLayout({ eyebrow, title, subtitle, children }: { eyebrow: string; title: string; subtitle: string; children: ReactNode }) {
  return <main className="auth-page"><section className="auth-panel"><div className="brand-lockup"><span className="brand-mark">+</span><span>CR<span className="brand-accent">+</span>CRM</span></div><div className="auth-copy"><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p>{subtitle}</p></div>{children}<div className="auth-foot"><span>Protected workspace</span><span>Your account, your data</span></div></section><aside className="auth-aside"><div className="aside-grid" /><div><p className="eyebrow">THE OUTREACH DESK</p><h2>Clear context.<br /><em>Better replies.</em></h2><p>One calm place for campaigns, conversations, and the people behind them.</p></div><div className="aside-stat"><strong>01</strong><span>Secure by design<br />Human approval always</span></div></aside></main>
}
