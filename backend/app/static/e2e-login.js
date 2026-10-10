// Login page, E2E mode only: drop any stored derived key (the session is over).
(function () {
  "use strict";
  try {
    const req = indexedDB.open("ai-remote-e2e", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("keys", { keyPath: "id" });
    req.onsuccess = () => {
      const db = req.result;
      try {
        const tx = db.transaction("keys", "readwrite");
        tx.objectStore("keys").delete("current");
        tx.oncomplete = tx.onerror = tx.onabort = () => db.close();
      } catch (error) {
        db.close();
      }
    };
  } catch (error) {
    /* storage unavailable: nothing to delete */
  }
})();
