/**
 * 官网（宣传页 `/` 与子页 `/about`）的站内链接与联系方式。
 *
 * 取自 Claude Design 原型「有据智评 宣传页.dc.html」「有据智评 官网子页.dc.html」。
 * 原型里的 `appUrl` 指向另一张设计稿，「联系我们」与「发现安全问题」的邮箱是占位
 * 地址；这里换成生产地址（用户决定，2026-10-17）。
 */

/** 工作台的规范地址。未登录时由工作台路由守卫转到登录页，登录后回到工作台首页。 */
export const APP_URL = "/workbench/";

export const ABOUT_URL = "/about";

/** 子页的三个分页；`contact` 是「院系部署」页里的锚点，不是独立分页。 */
export const ABOUT_PAGES = ["deploy", "security", "docs"];

export const CONTACT_EMAIL = "amir185715231@gmail.com";

/** 安全问题与日常联系用同一个邮箱（用户决定，2026-10-17）。 */
export const SECURITY_EMAIL = CONTACT_EMAIL;

/**
 * @param {"deploy"|"security"|"docs"|"contact"|"leads"} anchor
 */
export function aboutUrl(anchor) {
  return `${ABOUT_URL}#${anchor}`;
}

/**
 * @param {string} email
 * @param {string} [subject]
 */
export function mailto(email, subject) {
  return subject ? `mailto:${email}?subject=${encodeURIComponent(subject)}` : `mailto:${email}`;
}
