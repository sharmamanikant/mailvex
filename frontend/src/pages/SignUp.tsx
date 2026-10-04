import { useState } from 'react'
import type { FormEvent } from 'react'
import { authApi } from '../api/client'
import { AuthLayout } from './SignIn'

export default function SignUpPage() {
  const [name, setName] = useState('')
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
      const result = await authApi.signup(name, email, password)
      localStorage.setItem('crcrm_access_token', result.access_token)
      window.location.assign(nextPath?.startsWith('/') ? nextPath : '/dashboard')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to create your account')
    } finally {
      setPending(false)
    }
  }

  return <AuthLayout eyebrow="CREATE ACCOUNT" title="Start your workspace." subtitle="Set up your outreach account in minutes."><div className="custom-auth-card"><div className="custom-auth-heading"><h2>Create your workspace</h2><p>Use your email and a secure password.</p></div>{error && <div className="auth-error" role="alert">{error}</div>}<form className="custom-auth-form" onSubmit={(event) => void submit(event)}><label><span>Your name</span><input value={name} onChange={(event) => setName(event.target.value)} autoComplete="name" required placeholder="Your name" /></label><label><span>Email address</span><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="email" required placeholder="you@company.com" /></label><label><span>Password</span><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" minLength={8} required placeholder="At least 8 characters" /></label><button className="primary-button" type="submit" disabled={pending}>{pending ? 'Creating account...' : 'Create account'}</button></form><p className="custom-auth-switch">Already have an account? <a href="/sign-in">Sign in</a></p></div></AuthLayout>
}
