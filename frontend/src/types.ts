export type User = { id: number; username: string; companies: { id: number; code: string; name: string; role: string }[] }

export type Company = {
  id: number; code: string; name: string; inn: string; legal_form: string; industry: string
  first_operation_date: string | null; last_operation_date: string | null; operations_count: number
  min_balance: string; as_of_override: string | null; as_of: string | null; tax_regime: string
  external_code?: string; dataset_id?: string | null
}

export type DailyPoint = {
  date: string; p10: number; p50: number; p90: number; mean: number; prob_below: number
  det_in: number; det_out: number; stoch_in: number; stoch_out: number; is_business_day: boolean
}

export type ForecastEvent = {
  date: string; amount: number; direction: 'Поступление' | 'Списание'; category: string; label: string
  counterparty_name: string; source: string; confidence: number; pattern_key: string; probability?: number; timing_std_days?: number
}

export type Forecast = {
  as_of: string; opening_balance: number; model: string; scenario: any; min_balance: number
  daily: DailyPoint[]; events: ForecastEvent[]; data_quality: { level: string; history_days: number; stale_days: number | null; notes: string[] }
  components: Record<string, any>  // числа + probability_kind: 'uncalibrated_monte_carlo' | 'calibrated', calibration: {...}
  calculation_log?: { step: number; title: string; [key: string]: any }[]
}

export type Alert = { level: 'high' | 'medium' | 'low' | 'info'; kind: string; title: string; message: string; date: string | null; amount: number | null; probability: number | null; details: any }

export type Risk = {
  level: 'high' | 'medium' | 'low'; probability: number; gap_date: string | null; gap_date_p10: string | null
  deficit_p50: number; deficit_p10: number; runway_days: number | null; min_balance_p50: number
  explanation: string; summary: string
  waterfall: { label: string; amount: number; kind: string; date?: string; count?: number; note?: string }[]
  recommendations: { kind: string; title: string; text: string; amount: number | null }[]
  alerts: Alert[]
}

export type ForecastResponse = { forecast: Forecast; risk: Risk; run_id: number | null }

export type HistoryPoint = { date: string; inflow: number; outflow: number; balance: number }

export type Operation = {
  id: number; operation_id: string; operation_date: string; amount: string; direction: string
  counterparty_inn: string; counterparty_name: string; payment_purpose: string; balance_eod: string
  category: string; category_label: string; category_confidence: number; category_source: string
}

export type Category = { code: string; label: string; direction: 'in' | 'out'; kind: string; mandatory: boolean }

export type RecurringPattern = {
  id: number; key: string; category: string; category_label: string; direction: string; counterparty_name: string; label: string
  period: string; day_of_month: number | null; weekday: number | null; amount: string; amount_cv: number; occurrences: number
  first_date: string; last_date: string; confidence: number; probability: number; timing_std_days: number; purpose_hint: string
  active: boolean; user_disabled: boolean; history: { date: string; amount: number }[]
}

export type PlannedEvent = {
  id: number; kind: string; kind_label: string; direction: string; amount: string; date: string; issued_date: string | null; due_date: string | null
  counterparty_inn: string; counterparty_name: string; category: string; category_label: string; label: string; source: string
  probability: number; timing_std_days: number; status: string; matched_date: string | null; note: string; meta: any
}

export type Contract = {
  id: number; number: string; counterparty_inn: string; counterparty_name: string; direction: string; total_amount: string
  start_date: string; end_date: string | null; schedule_type: string; schedule_label: string; prepayment_pct: number; deferral_days: number
  installments: number; day_of_month: number | null; milestones: any[]; category: string; note: string; active: boolean; created_at: string
  events: { id: number; date: string; amount: number; label: string; status: string; probability: number; timing_std_days: number }[]
}

export type Scenario = {
  name?: string; revenue_change_pct?: number; fixed_cost_change_pct?: number; delay_client_days?: number; delay_client_key?: string | null
  extra_expenses?: { date: string; amount: number; label: string }[]; extra_incomes?: { date: string; amount: number; label: string }[]
}
