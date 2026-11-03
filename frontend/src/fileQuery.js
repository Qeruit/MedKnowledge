// Date inputs use '' when cleared; the API expects an ISO date or null.
export function filePageQuery(current, size, filter) {
  return {
    current,
    size,
    fileName: filter.fileName || null,
    reviewStatus: filter.reviewStatus || null,
    startDate: filter.startDate || null,
    endDate: filter.endDate || null,
  }
}
