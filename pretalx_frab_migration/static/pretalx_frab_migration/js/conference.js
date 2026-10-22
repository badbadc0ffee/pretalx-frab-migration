const conferenceForm = document.querySelector("[data-frab-conference]")
if (conferenceForm) {
    conferenceForm.elements.conference.addEventListener("change", () => {
        conferenceForm.requestSubmit(conferenceForm.querySelector("[data-select-conference]"))
    })
    const slugField = conferenceForm.elements.slug
    if (slugField) {
        slugField.addEventListener("change", () => {
            conferenceForm.requestSubmit(conferenceForm.querySelector("[data-check-slug]"))
        })
    }
}

const existingEvent = conferenceForm?.elements.existing_event
if (existingEvent) {
    existingEvent.addEventListener("change", () => {
        conferenceForm.requestSubmit(conferenceForm.querySelector("[data-check-slug]"))
    })
}
