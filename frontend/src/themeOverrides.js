// naive-ui theme overrides, kept in sync with the CSS tokens in styles.css.
// Light: fine borders + a whisper of shadow. Dark: layered surfaces only.

const commonLight = {
  primaryColor: '#4c5fd5',
  primaryColorHover: '#3e4ec4',
  primaryColorPressed: '#3e4ec4',
  primaryColorSuppl: '#4c5fd5',
  infoColor: '#2f6fbe',
  successColor: '#1d8a4e',
  warningColor: '#b57a00',
  errorColor: '#d03050',
  borderRadius: '8px',
  bodyColor: '#f5f6fb',
  cardColor: '#ffffff',
  modalColor: '#ffffff',
  popoverColor: '#ffffff',
  tableColor: '#ffffff',
  inputColor: '#ffffff',
  textColorBase: '#1f2937',
  textColor1: '#141829',
  textColor2: '#1f2937',
  textColor3: '#5f6b7a',
  placeholderColor: '#5f6b7a',
  borderColor: '#d7dae6',
  dividerColor: '#e6e8f0',
  focusOutline: '#4c5fd5',
}

const commonDark = {
  primaryColor: '#8b93ff',
  primaryColorHover: '#a5abff',
  primaryColorPressed: '#6e77e8',
  primaryColorSuppl: '#8b93ff',
  infoColor: '#6fa8f0',
  successColor: '#4bc47e',
  warningColor: '#e0a93c',
  errorColor: '#f26d82',
  borderRadius: '8px',
  bodyColor: '#10121a',
  cardColor: '#171a24',
  modalColor: '#1d2130',
  popoverColor: '#1d2130',
  tableColor: '#171a24',
  inputColor: '#1d2130',
  textColorBase: '#e6e8f0',
  textColor1: '#f4f5fa',
  textColor2: '#e6e8f0',
  textColor3: '#9aa3b5',
  placeholderColor: '#9aa3b5',
  borderColor: '#343a4e',
  dividerColor: '#262b3a',
  focusOutline: '#8b93ff',
}

export const lightThemeOverrides = {
  common: commonLight,
  Button: {
    borderRadiusMedium: '8px',
    borderRadiusSmall: '6px',
    // Primary buttons keep white text on the indigo-blue fill.
    textColorPrimary: '#ffffff',
  },
  Card: { borderRadius: '10px' },
  DataTable: {
    thColor: '#f8f9fd',
    thTextColor: '#5f6b7a',
    tdColorHover: '#f8f9fd',
    borderColor: '#e6e8f0',
  },
  Menu: {
    itemTextColorActive: '#3e4ec4',
    itemColorActive: '#eef0fc',
    itemColorActiveHover: '#eef0fc',
    itemColorActiveCollapsed: '#eef0fc',
  },
  Modal: { borderRadius: '10px' },
  Popconfirm: { borderRadius: '8px' },
  Descriptions: { thColor: '#f8f9fd', tdColor: '#ffffff' },
  Statistic: { valueFontWeight: '700', labelFontSize: '12px' },
  Tag: { borderRadius: '6px' },
  Alert: { borderRadius: '8px' },
}

export const darkThemeOverrides = {
  common: commonDark,
  Button: {
    borderRadiusMedium: '8px',
    borderRadiusSmall: '6px',
    // The bright indigo fill needs dark text to hold >= 4.5:1.
    textColorPrimary: '#0e0f1a',
  },
  Card: { borderRadius: '10px' },
  DataTable: {
    thColor: '#1d2130',
    thTextColor: '#9aa3b5',
    tdColorHover: '#1d2130',
    borderColor: '#262b3a',
  },
  Menu: {
    itemTextColorActive: '#8b93ff',
    itemColorActive: '#232741',
    itemColorActiveHover: '#232741',
    itemColorActiveCollapsed: '#232741',
  },
  Modal: { borderRadius: '10px' },
  Popconfirm: { borderRadius: '8px' },
  Descriptions: { thColor: '#1d2130', tdColor: '#171a24' },
  Statistic: { valueFontWeight: '700', labelFontSize: '12px' },
  Tag: { borderRadius: '6px' },
  Alert: { borderRadius: '8px' },
}
