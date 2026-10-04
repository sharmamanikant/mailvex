import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import SignInPage from './SignIn'

describe('SignIn', () => {
  it('renders the branded email sign-in form', () => {
    render(<SignInPage />)
    expect(screen.getByText('WORKSPACE ACCESS')).toBeInTheDocument()
    expect(screen.getByLabelText('Email address')).toBeInTheDocument()
    expect(screen.getByText(/Make every message count/)).toBeInTheDocument()
  })

  it('surfaces the protected-workspace footer', () => {
    render(<SignInPage />)
    expect(screen.getByText('Protected workspace')).toBeInTheDocument()
    expect(screen.getByText('Your account, your data')).toBeInTheDocument()
  })
})