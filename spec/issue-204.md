<img width="854" height="942" alt="Image" src="https://github.com/user-attachments/assets/9bf9293d-6ca1-46ce-b77d-cae6ee067480" />

### **Functional or Technical Spike**
Even on a B2 instance the app is getting real close to the maximum memory (see screenshot) when making multiple API calls. We should look into if we need to upgrade the instance by one more or that we can use the instance more efficiently.

Discussed with Daan, the P0v3 (P0v4 is not currently available in the current zone) would be a better pick. After running production by users, we can see if P1v3 is required or not. 

On production we want to have P0v3, on dev we can keep B2.

### **Acceptance Criteria**
Describe when the expected output of this spike should be accepted.
- [ ] Decide if we want to upgrade to a higher instance or that we can use the instance more efficiently.
