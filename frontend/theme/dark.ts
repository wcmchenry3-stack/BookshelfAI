import { tokens } from './tokens';
import { colorsFor, type Theme } from './light';

export const darkTheme: Theme = {
  colors: colorsFor('dark'),
  typography: tokens.typography,
  spacing: tokens.spacing,
  radius: tokens.radius,
  touchTarget: tokens.touchTarget,
  isDark: true,
};
