
### For Distributions Ubuntu and Debian

**DOWNLOAD COMMAND:** 

```
git clone https://github.com/alexis-82/sqemu64.git
```

**INSTALLATION:**

```
Installing dependencies with option number 13
```

other with Ansible:

```bash
ansible-playbook install_packages.yml
```


**NEXT STEP:** 
(user root)

```
./sqemu64
```

**NEW FEATURES:**
- MAC Address Random for multiple networks
- Options kvm for high performance of the vm
- USB connection function
- Interactive shell for system minimal

**SUPPORT**  
Executive error of interpreter python3 "^M":
```bash
apt install dos2unix
dos2unix sqemu64
```

System Logs:
```bash
./sqemu64 2>&1 | tee -a sqemu64.log
```

**SCREENSHOT**

[![Istantanea-2022-01-30-22-10-35.png](https://i.postimg.cc/rpdyR328/Istantanea-2022-01-30-22-10-35.png)](https://postimg.cc/06s1LtN3)


