import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import SignUpPage from './SignUp'

describe('SignUp', () => {
  it('renders the branded email sign-up form', () => {
    render(<SignUpPage />)
    expect(screen.getByText('CREATE ACCOUNT')).toBeInTheDocument()
    expect(screen.getByLabelText('Your name')).toBeInTheDocument()
    expect(screen.getByText(/Start your workspace/)).toBeInTheDocument()
  })

  it('surfaces the protected-workspace footer', () => {
    render(<SignUpPage />)
    expect(screen.getByText('Protected workspace')).toBeInTheDocument()
  })
})