/*
    LOGIN
*/

let rejectEmergencyId = null;

const loginForm = document.getElementById("loginForm");

if (loginForm) {

    loginForm.addEventListener("submit", async function(event) {

        event.preventDefault();

        const id =
            document.getElementById("employeeId").value;

        const password =
            document.getElementById("password").value;

        const message =
            document.getElementById("loginMessage");

        try {

            const response = await fetch("/api/login", {

                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({
                    id,
                    password
                })

            });

            const data = await response.json();

            if (!data.success) {

                message.textContent = data.message;

                return;
            }

            /*
                Store logged-in user
            */

            localStorage.setItem(
                "currentUser",
                JSON.stringify(data.user)
            );


            /*
                Separate homepage
                depending on role
            */

            if (data.user.role === "supervisor") {

                window.location.href =
                    "supervisor.html";

            } else {

                window.location.href =
                    "worker.html";

            }

        } catch (error) {

            message.textContent =
                "Unable to connect to server.";

            console.error(error);

        }

    });

}


/*
    GET CURRENT USER
*/

function getCurrentUser() {

    const user =
        localStorage.getItem("currentUser");

    if (!user) {

        window.location.href = "login.html";

        return null;
    }

    return JSON.parse(user);
}


/*
    WORKER PAGE
*/

function getEmergencyGuidance(type) {
    const normalizedType = String(type || "").trim().toLowerCase();

    const guidanceMap = {
        fire: "Please prioritize your safety and leave the building immediately.",
        injury: "Please move to a safe area and seek first aid immediately.",
        "equipment failure": "Please stop using the equipment and move away from the area.",
        "electrical hazard": "Please stay away from the hazard and avoid water or electrical sources.",
        "medical emergency": "Please get immediate medical help and stay with the person if it is safe to do so.",
        "need more time": "We added time. Please check your schedule for the added time.",
        "need more tim": "We added time. Please check your schedule for the added time.",
        other: "Please move to a safe place and follow the supervisor's instructions."
    };

    return guidanceMap[normalizedType] || "Please prioritize your safety and follow the supervisor's instructions immediately.";
}

if (window.location.pathname.includes("worker.html")) {

    const user = getCurrentUser();

    if (user) {

        document.getElementById("workerName")
            .textContent =
            `Welcome, ${user.name}`;

        document.getElementById("workerDetails")
            .textContent =
            `Employee ID: ${user.id} | Department: ${user.department}`;

        loadWorkerEmergencyStatus();
        setInterval(loadWorkerEmergencyStatus, 3000);

    }

}

async function loadWorkerEmergencyStatus() {
    const user = getCurrentUser();

    if (!user) return;

    try {
        const response = await fetch("/api/emergencies");
        const emergencies = await response.json();
        const workerAlert = emergencies
            .filter(emergency => emergency.employeeId === user.id)
            .sort((a, b) => Number(b.id) - Number(a.id))
            .find(emergency => ["ACKNOWLEDGED", "REJECTED"].includes(emergency.status));

        const statusBox = document.getElementById("workerEmergencyStatus");
        const statusText = document.getElementById("workerEmergencyStatusText");

        if (!statusBox || !statusText) return;

        if (!workerAlert) {
            statusBox.classList.add("hidden");
            return;
        }

        if (workerAlert.status === "ACKNOWLEDGED") {
            statusText.textContent = `Supervisor update: ${workerAlert.adminMessage || getEmergencyGuidance(workerAlert.type)}`;
        } else {
            statusText.textContent = `Supervisor rejected your emergency. Reason: ${workerAlert.rejectReason || "No reason provided."}`;
        }

        statusBox.classList.remove("hidden");

    } catch (error) {
        console.error(error);
    }
}


/*
    SHOW EMERGENCY FORM
*/

function showEmergencyForm() {

    const form =
        document.getElementById("emergencyForm");

    if (form) {

        form.classList.remove("hidden");

        form.scrollIntoView({
            behavior: "smooth"
        });

    }

}


/*
    HIDE EMERGENCY FORM
*/

function hideEmergencyForm() {

    const form =
        document.getElementById("emergencyForm");

    if (form) {

        form.classList.add("hidden");

    }

}


/*
    SUBMIT EMERGENCY
*/

async function submitEmergency() {

    const user = getCurrentUser();

    if (!user) return;


    const type =
        document.getElementById("emergencyType").value;

    const description =
        document.getElementById("emergencyDescription").value;

    const message =
        document.getElementById("emergencyMessage");


    if (!type || !description.trim()) {

        message.textContent =
            "Please provide the emergency type and description.";

        return;
    }


    try {

        const response = await fetch(
            "/api/emergencies",
            {

                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify({

                    employeeId: user.id,

                    employeeName: user.name,

                    type: type,

                    description: description

                })

            }
        );


        const data = await response.json();


        if (data.success) {

            message.textContent =
                "Emergency alert sent to your supervisor.";

            document.getElementById(
                "emergencyType"
            ).value = "";

            document.getElementById(
                "emergencyDescription"
            ).value = "";

        } else {

            message.textContent =
                data.message;

        }

    } catch (error) {

        message.textContent =
            "Unable to send emergency.";

        console.error(error);

    }

}


/*
    SUPERVISOR PAGE
*/

if (
    window.location.pathname.includes(
        "supervisor.html"
    )
) {

    const user = getCurrentUser();

    if (user) {

        document.getElementById("supervisorName")
            .textContent =
            `Welcome, ${user.name}`;

        const rejectModal = document.getElementById("rejectModal");
        const rejectInput = document.getElementById("rejectReasonInput");
        const cancelReject = document.getElementById("cancelReject");
        const confirmReject = document.getElementById("confirmReject");

        if (cancelReject) {
            cancelReject.addEventListener("click", () => {
                rejectEmergencyId = null;
                rejectInput.value = "";
                rejectModal.classList.add("hidden");
            });
        }

        if (confirmReject) {
            confirmReject.addEventListener("click", async () => {
                const reason = rejectInput.value.trim();

                if (!reason) {
                    alert("A rejection reason is required.");
                    rejectInput.focus();
                    return;
                }

                if (!rejectEmergencyId) return;

                rejectModal.classList.add("hidden");
                rejectInput.value = "";

                await updateEmergencyStatus(rejectEmergencyId, "REJECT", reason);
                rejectEmergencyId = null;
            });
        }

        loadEmployees();

        loadEmergencies();

        /*
            Refresh emergency alerts
            every 3 seconds
        */

        setInterval(
            loadEmergencies,
            3000
        );

    }

}


/*
    LOAD EMPLOYEES
*/

async function loadEmployees() {

    try {

        const response =
            await fetch("/api/employees");

        const employees =
            await response.json();

        const employeeList =
            document.getElementById("employeeList");

        const employeeCount =
            document.getElementById("employeeCount");


        if (!employeeList) return;


        employeeCount.textContent =
            employees.length;


        employeeList.innerHTML = "";


        employees.forEach(employee => {

            const card =
                document.createElement("div");

            card.className =
                "employee-card";


            card.innerHTML = `

                <div>

                    <strong>
                        ${employee.name}
                    </strong>

                    <p>
                        ID: ${employee.id}
                    </p>

                </div>

                <div>

                    <span>
                        ${employee.role}
                    </span>

                    <p>
                        ${employee.department}
                    </p>

                </div>

            `;


            employeeList.appendChild(card);

        });


    } catch (error) {

        console.error(error);

    }

}


/*
    LOAD EMERGENCIES
*/

async function loadEmergencies() {

    try {

        const response =
            await fetch("/api/emergencies");

        const emergencies =
            await response.json();


        const list =
            document.getElementById("emergencyList");

        const activeCount =
            document.getElementById(
                "activeEmergencyCount"
            );


        if (!list) return;


        const activeEmergencies =
            emergencies.filter(
                emergency =>
                    emergency.status === "ACTIVE"
            );


        activeCount.textContent =
            activeEmergencies.length;


        if (emergencies.length === 0) {

            list.innerHTML = `
                <div class="no-emergency">
                    No emergency alerts.
                </div>
            `;

            return;
        }


        list.innerHTML = "";


        /*
            Newest emergency first
        */

        emergencies
            .slice()
            .reverse()
            .forEach(emergency => {

                const card =
                    document.createElement("div");


                const isOpen = emergency.status === "ACTIVE";

                card.className =
                    isOpen
                        ? "emergency-alert"
                        : "emergency-alert acknowledged";


                card.innerHTML = `

                    <div class="alert-header">

                        <h3>
                            🚨 ${emergency.type}
                        </h3>

                        <span>
                            ${emergency.status}
                        </span>

                    </div>


                    <p>
                        <strong>
                            Employee:
                        </strong>

                        ${emergency.employeeName}
                    </p>


                    <p>
                        <strong>
                            Employee ID:
                        </strong>

                        ${emergency.employeeId}
                    </p>


                    <p>
                        <strong>
                            Description:
                        </strong>

                        ${emergency.description}
                    </p>


                    <p>
                        <strong>
                            Time:
                        </strong>

                        ${emergency.time}
                    </p>

                    ${
                        emergency.rejectReason
                            ? `<p><strong>Rejection Reason:</strong> ${emergency.rejectReason}</p>`
                            : ""
                    }

                    ${
                        isOpen

                        ?

                        `
                            <div class="button-row">
                                <button
                                    class="acknowledge-button"
                                    onclick="acknowledgeEmergency(${emergency.id})"
                                >
                                    Acknowledge
                                </button>

                                <button
                                    class="reject-button"
                                    onclick="rejectEmergency(${emergency.id})"
                                >
                                    Reject
                                </button>
                            </div>
                        `

                        :

                        ""
                    }

                `;


                list.appendChild(card);

            });


    } catch (error) {

        console.error(error);

    }

}


/*
    UPDATE EMERGENCY STATUS
*/

async function updateEmergencyStatus(id, action, reason = "", message = "") {

    try {

        const response = await fetch(
            `/api/emergencies/${id}`,
            {
                method: "PUT",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({ action, reason, message })
            }
        );

        const result = await response.json();

        if (!response.ok) {
            alert(result.message || "Unable to update emergency status.");
            return;
        }

        loadEmergencies();

    } catch (error) {

        console.error(error);

    }

}

async function acknowledgeEmergency(id) {

    const emergencies = await fetch("/api/emergencies").then(response => response.json());
    const selectedEmergency = emergencies.find(item => item.id === id);
    const message = selectedEmergency
        ? getEmergencyGuidance(selectedEmergency.type)
        : "Please prioritize your safety and follow the supervisor's instructions immediately.";

    await updateEmergencyStatus(id, "ACKNOWLEDGE", "", message);

}

async function rejectEmergency(id) {

    const rejectModal = document.getElementById("rejectModal");
    const rejectInput = document.getElementById("rejectReasonInput");

    if (!rejectModal || !rejectInput) {
        return;
    }

    rejectEmergencyId = id;
    rejectInput.value = "";
    rejectModal.classList.remove("hidden");
    rejectInput.focus();

}


/*
    LOGOUT
*/

function logout() {

    localStorage.removeItem(
        "currentUser"
    );

    window.location.href =
        "login.html";

}