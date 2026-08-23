export const state = {
  bootstrap: null,
  page: "dashboard",
  pageVersion: 0,
  selected: new Set(),
  selectedDrafts: new Set(),
  draftView: "table",
  currentBatchId: null,
  quotation: null,
  quotationNodeSequence: 0,
  quotationDescriptionEnabled: false,
  quotationExportMeta: {
    title: "项目开发报价单",
    client_name: "",
    project_manager: "",
    quote_date: new Date().toISOString().slice(0, 10),
  },
};
