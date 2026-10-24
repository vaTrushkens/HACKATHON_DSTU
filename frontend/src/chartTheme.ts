// Tooltips live outside scrollable cards so following the pointer cannot
// expand the card. Confinement and wrapping keep them inside small viewports.
export const chartTooltip = {
  confine: true,
  appendToBody: true,
  transitionDuration: 0,
  hideDelay: 0,
  className: 'chart-tooltip',
  backgroundColor: '#ffffff',
  borderColor: '#e1e4eb',
  borderWidth: 1,
  borderRadius: 6,
  padding: [10, 14],
  textStyle: { color: '#292929', fontFamily: 'Arial, Helvetica, sans-serif', fontSize: 12 },
  extraCssText: 'max-width:min(320px,calc(100vw - 96px));white-space:normal;overflow-wrap:anywhere;box-shadow:0 6px 20px #292e3c14;',
}
