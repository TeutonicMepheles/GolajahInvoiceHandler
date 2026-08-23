import { $$ } from "./dom.js";

export function FormDataForm(root) {
  const entries = [];
  $$('input,select,textarea', root).forEach((field) => {
    if (field.name) entries.push([field.name, field.type === "checkbox" ? field.checked : field.value]);
  });
  return entries;
}
